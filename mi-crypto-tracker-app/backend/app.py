from flask import Flask, request, jsonify
from flask_cors import CORS
import csv
import os
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.background import BackgroundScheduler
import asyncio
import httpx
import sys # Importar sys para manejar el bucle de eventos

app = Flask(__name__)
CORS(app)

# --- BACKEND CONFIGURATION ---
KUCOIN_INTERVAL = "1hour"
KUCOIN_LIMIT = 200

SAVE_REC_TO_BACKEND_INTERVAL = timedelta(hours=1)
PRICE_CHANGE_THRESHOLD = 0.03

CSV_FILE = 'data.csv'
LAST_REC_FILE = 'last_recommendations.csv'

current_analysis_cache = {}

SYMBOLS_TO_MONITOR = []

# --- CSV UTILITY FUNCTIONS ---
def ensure_csv_exists():
    """Ensures the CSV files for data and last recommendations exist, creating them with headers if not."""
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(['timestamp', 'symbol', 'recommendation', 'prev_recommendation', 'metric_type', 'metric_value', 'details'])

    if not os.path.exists(LAST_REC_FILE):
        with open(LAST_REC_FILE, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(['symbol', 'timestamp', 'recommendation', 'sma_rec', 'rsi_rec', 'bb_rec', 'last_price'])

ensure_csv_exists()

def get_last_recommendation_from_file(symbol):
    """Retrieves the last saved recommendation for a given symbol from the CSV file."""
    if not os.path.exists(LAST_REC_FILE):
        return None
    with open(LAST_REC_FILE, mode='r', newline='', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        for row in reader:
            if row['symbol'] == symbol:
                return row
    return None

def update_last_recommendation_file(symbol, timestamp_iso, recommendation, sma_rec, rsi_rec, bb_rec, current_price):
    """Updates or adds the last recommendation for a symbol in the CSV file."""
    rows = []
    found = False
    if os.path.exists(LAST_REC_FILE):
        with open(LAST_REC_FILE, mode='r', newline='', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            rows = list(reader)

    updated_rows = []
    for row in rows:
        if row['symbol'] == symbol:
            updated_rows.append({
                'symbol': symbol,
                'timestamp': timestamp_iso,
                'recommendation': recommendation,
                'sma_rec': sma_rec,
                'rsi_rec': rsi_rec,
                'bb_rec': bb_rec,
                'last_price': current_price
            })
            found = True
        else:
            updated_rows.append(row)

    if not found:
        updated_rows.append({
            'symbol': symbol,
            'timestamp': timestamp_iso,
            'recommendation': recommendation,
            'sma_rec': sma_rec,
            'rsi_rec': rsi_rec,
            'bb_rec': bb_rec,
            'last_price': current_price
        })

    with open(LAST_REC_FILE, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=['symbol', 'timestamp', 'recommendation', 'sma_rec', 'rsi_rec', 'bb_rec', 'last_price'])
        writer.writeheader()
        writer.writerows(updated_rows)

# --- NEW FUNCTION: Get ALL KuCoin Symbols ---
async def get_all_kucoin_symbols():
    """Fetches all tradable symbols from the KuCoin API, filtering for USDT/USDC pairs."""
    url = "https://api.kucoin.com/api/v1/symbols"
    print(f"[{datetime.now().isoformat()}] Fetching all symbols from KuCoin API: {url}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=15.0)
            response.raise_for_status()

            data = response.json()

            if not data or not data.get('data') or not isinstance(data['data'], list):
                raise ValueError("KuCoin API for symbols returned invalid or no data.")

            filtered_symbols = []
            for item in data['data']:
                if item.get('enableTrading') and item.get('baseCurrency') and item.get('quoteCurrency'):
                    if item['quoteCurrency'] == 'USDT' or item['quoteCurrency'] == 'USDC':
                        filtered_symbols.append(f"{item['baseCurrency']}-{item['quoteCurrency']}")

            filtered_symbols = sorted(list(set(filtered_symbols)))
            print(f"[{datetime.now().isoformat()}] Fetched {len(filtered_symbols)} symbols from KuCoin.")
            print(f"[{datetime.now().isoformat()}] First 10 symbols: {filtered_symbols[:10]}")
            return filtered_symbols

    except httpx.HTTPStatusError as e:
        print(f"HTTP Error fetching symbols from KuCoin: {e.response.status_code} - {e.response.text}")
        return []
    except httpx.RequestError as e:
        print(f"Network error fetching symbols from KuCoin: {e}")
        return []
    except ValueError as e:
        print(f"KuCoin data error for symbols: {e}")
        return []
    except Exception as e:
        print(f"Unexpected error fetching symbols from KuCoin: {e}")
        return []


# --- DATA RETRIEVAL FUNCTIONS (KUCOIN API for Klines) ---
async def get_kucoin_klines(symbol, interval=KUCOIN_INTERVAL, limit=KUCOIN_LIMIT):
    """Fetches candlestick data (klines) for a given symbol from the KuCoin API."""
    kucoin_symbol = symbol
    url = f"https://api.kucoin.com/api/v1/market/candles?symbol={kucoin_symbol}&type={interval}&limit={limit}"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()

            data = response.json()

            if not data or not data.get('data') or not isinstance(data['data'], list) or len(data['data']) == 0:
                raise ValueError(f"KuCoin API for {kucoin_symbol} returned valid response but no candlestick data.")

            formatted_prices = []
            for kline in data['data']:
                formatted_prices.append({
                    'x': int(kline[0]) * 1000, # Timestamp in milliseconds
                    'y': float(kline[2])      # Closing price
                })

            return formatted_prices[::-1] # Reverse to have oldest first

    except httpx.HTTPStatusError as e:
        print(f"KuCoin HTTP Error for {kucoin_symbol}: {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"Network error connecting to KuCoin for {kucoin_symbol}: {e}")
        return None
    except ValueError as e:
        print(f"KuCoin data error for {kucoin_symbol}: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error fetching KuCoin data for {kucoin_symbol}: {e}")
        return None


# --- INDICATOR CALCULATION FUNCTIONS (Unchanged) ---
def calculate_sma(data, period):
    """Calculates Simple Moving Average (SMA) for given data."""
    sma = []
    if not data or len(data) < period:
        return [None] * len(data) if data else []

    for i in range(len(data)):
        if i < period - 1:
            sma.append(None)
        else:
            slic = data[i - period + 1 : i + 1]
            sum_val = sum(slic)
            sma.append({'y': sum_val / period})
    return sma

def calculate_bollinger_bands(data, period, std_dev_multiplier):
    """Calculates Bollinger Bands (BB) for given data."""
    middle = []
    upper = []
    lower = []
    if not data or len(data) < period:
        nulls = [None] * len(data) if data else []
        return {'middle': nulls, 'upper': nulls, 'lower': nulls}

    for i in range(len(data)):
        if i < period - 1:
            middle.append(None)
            upper.append(None)
            lower.append(None)
        else:
            slic = data[i - period + 1 : i + 1]
            mean = sum(slic) / period
            std_dev = (sum((x - mean) ** 2 for x in slic) / period) ** 0.5

            middle.append({'y': mean})
            upper.append({'y': mean + (std_dev * std_dev_multiplier)})
            lower.append({'y': mean - (std_dev * std_dev_multiplier)})
    return {'middle': middle, 'upper': upper, 'lower': lower}

def calculate_rsi(data, period):
    """Calculates Relative Strength Index (RSI) for given data."""
    rsi_values = []

    if not data or len(data) < period + 1:
        return [None] * len(data) if data else []

    # Initialize with None for the first 'period' entries
    for _ in range(period):
        rsi_values.append(None)

    gains = []
    losses = []
    for i in range(1, len(data)):
        diff = data[i] - data[i - 1]
        gains.append(diff if diff > 0 else 0)
        losses.append(abs(diff) if diff < 0 else 0)

    # Calculate initial average gain and loss
    avg_gain = sum(gains[0:period]) / period
    avg_loss = sum(losses[0:period]) / period

    if avg_loss == 0:
        rsi_values.append({'y': 100.0})
    else:
        rs = avg_gain / avg_loss
        rsi_values.append({'y': 100 - (100 / (1 + rs))})

    # Calculate subsequent RSI values
    for i in range(period, len(gains)):
        current_gain = gains[i]
        current_loss = losses[i]

        avg_gain = ((avg_gain * (period - 1)) + current_gain) / period
        avg_loss = ((avg_loss * (period - 1)) + current_loss) / period

        if avg_loss == 0:
            rsi_values.append({'y': 100.0})
        else:
            rs = avg_gain / avg_loss
            rsi_values.append({'y': 100 - (100 / (1 + rs))})
    return rsi_values

# --- Combined Signals Logic ---
def get_combined_signals(sma_short, sma_long, rsi, bollinger_bands, closing_prices):
    """Determines combined trading signals based on SMA, RSI, and Bollinger Bands."""
    sma_rec = 'hold'
    rsi_rec = 'hold'
    bb_rec = 'hold'

    valid_sma_short = [v['y'] for v in sma_short if v is not None]
    valid_sma_long = [v['y'] for v in sma_long if v is not None]
    if len(valid_sma_short) >= 2 and len(valid_sma_long) >= 2:
        last_sma_short = valid_sma_short[-1]
        prev_sma_short = valid_sma_short[-2]
        last_sma_long = valid_sma_long[-1]
        prev_sma_long = valid_sma_long[-2]
        if prev_sma_short <= prev_sma_long and last_sma_short > last_sma_long:
            sma_rec = 'buy'
        elif prev_sma_short >= prev_sma_long and last_sma_short < last_sma_long:
            sma_rec = 'sell'
    else:
        sma_rec = 'N/A'

    valid_rsi = [v['y'] for v in rsi if v is not None]
    if len(valid_rsi) > 0:
        last_rsi = valid_rsi[-1]
        if last_rsi > 70:
            rsi_rec = 'sell'
        elif last_rsi < 30:
            rsi_rec = 'buy'
    else:
        rsi_rec = 'N/A'

    valid_bb_upper = [v['y'] for v in bollinger_bands['upper'] if v is not None]
    valid_bb_lower = [v['y'] for v in bollinger_bands['lower'] if v is not None]
    last_price_val = closing_prices[-1] if closing_prices else None

    if len(valid_bb_upper) > 0 and len(valid_bb_lower) > 0 and last_price_val is not None:
        last_bb_upper = valid_bb_upper[-1]
        last_bb_lower = valid_bb_lower[-1]
        if last_price_val > last_bb_upper:
            bb_rec = 'sell'
        elif last_price_val < last_bb_lower:
            bb_rec = 'buy'
    else:
        bb_rec = 'N/A'

    buy_count = 0
    sell_count = 0
    na_count = 0

    if sma_rec == 'buy': buy_count += 1
    elif sma_rec == 'sell': sell_count += 1
    else: na_count += 1

    if rsi_rec == 'buy': buy_count += 1
    elif rsi_rec == 'sell': sell_count += 1
    else: na_count += 1

    if bb_rec == 'buy': buy_count += 1
    elif bb_rec == 'sell': sell_count += 1
    else: na_count += 1

    overall_recommendation = 'hold'

    if (buy_count + sell_count) >= 2:
        if buy_count >= 2 and sell_count == 0:
            overall_recommendation = 'buy'
        elif sell_count >= 2 and buy_count == 0:
            overall_recommendation = 'sell'
        else:
            overall_recommendation = 'hold'
    else:
        overall_recommendation = 'hold'

    return {'sma': sma_rec, 'rsi': rsi_rec, 'bb': bb_rec, 'overall': overall_recommendation}


# --- SCHEDULED TASK TO FETCH AND ANALYZE DATA ---
async def scheduled_analysis_job(symbols):
    """
    Scheduled job to fetch klines, calculate indicators, and determine trading signals
    for a list of symbols. Results are cached and saved to CSV if conditions are met.
    """
    print(f"[{datetime.now().isoformat()}] Scheduled job started for {len(symbols)} symbols.")
    for symbol in symbols:
        try:
            print(f"[{datetime.now().isoformat()}] Analyzing {symbol}...")
            klines_data = await get_kucoin_klines(symbol)

            min_required_klines = max(20, 50, 14) + 1 # Min klines needed for indicators
            if not klines_data or len(klines_data) < min_required_klines:
                print(f"[{datetime.now().isoformat()}] Insufficient data for {symbol}. Needed {min_required_klines}, got {len(klines_data) if klines_data else 0}. Skipping analysis.")
                current_overall_rec = 'hold'
                individual_recs = {'sma': 'N/A', 'rsi': 'N/A', 'bb': 'N/A'}
                current_price = klines_data[-1]['y'] if klines_data and len(klines_data) > 0 else 0.0
            else:
                closing_prices = [p['y'] for p in klines_data]
                current_price = closing_prices[-1]

                sma_short = calculate_sma(closing_prices, 20)
                sma_long = calculate_sma(closing_prices, 50)
                bollinger_bands = calculate_bollinger_bands(closing_prices, 20, 2)
                rsi = calculate_rsi(closing_prices, 14)

                combined_signals = get_combined_signals(sma_short, sma_long, rsi, bollinger_bands, closing_prices)
                current_overall_rec = combined_signals['overall']
                individual_recs = {'sma': combined_signals['sma'], 'rsi': combined_signals['rsi'], 'bb': combined_signals['bb']}

            # Update cache with full results for this symbol
            current_analysis_cache[symbol] = {
                'overall_rec': current_overall_rec,
                'sma': individual_recs['sma'],
                'rsi': individual_recs['rsi'],
                'bb': individual_recs['bb'],
                'klines': klines_data,
                'sma_short': sma_short,
                'sma_long': sma_long,
                'bb_bands': bollinger_bands,
                'rsi_data': rsi
            }

            # Decide whether to save the recommendation (1 hour / 3% change logic)
            last_rec_info = get_last_recommendation_from_file(symbol)

            should_save = False
            now_dt = datetime.now(timezone.utc)

            if last_rec_info:
                last_saved_timestamp = datetime.fromisoformat(last_rec_info['timestamp'].replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
                last_saved_price = float(last_rec_info.get('last_price', 0.0))

                has_time_passed = (now_dt - last_saved_timestamp) >= SAVE_REC_TO_BACKEND_INTERVAL

                has_significant_price_change = False
                if last_saved_price != 0.0 and current_price is not None and current_price != 0:
                    percentage_change = abs(current_price - last_saved_price) / last_saved_price
                    has_significant_price_change = percentage_change >= PRICE_CHANGE_THRESHOLD
                    print(f"  {symbol}: Price change: {(percentage_change*100):.2f}% (Threshold: {PRICE_CHANGE_THRESHOLD*100:.0f}%), Time passed: {has_time_passed}")
                else:
                     has_significant_price_change = True # If no previous price, save it

                if has_time_passed or has_significant_price_change:
                    should_save = True
            else: # First recommendation for this symbol
                should_save = True
                print(f"  {symbol}: First recommendation, saving.")

            if should_save:
                last_prev_rec = last_rec_info.get('recommendation', 'N/A') if last_rec_info else 'N/A'
                last_prev_sma_rec = last_rec_info.get('sma_rec', 'N/A') if last_rec_info else 'N/A'
                last_prev_rsi_rec = last_rec_info.get('rsi_rec', 'N/A') if last_rec_info else 'N/A'
                last_prev_bb_rec = last_rec_info.get('bb_rec', 'N/A') if last_rec_info else 'N/A'

                metric_type = 'N/A'
                metric_value = 0.0
                details = ""

                if last_prev_rec != 'N/A' and current_overall_rec != 'N/A':
                    if current_overall_rec == last_prev_rec:
                        metric_type = 'Acierto'
                        match_count = 0
                        if individual_recs['sma'] == last_prev_sma_rec and individual_recs['sma'] != 'N/A': match_count += 1
                        if individual_recs['rsi'] == last_prev_rsi_rec and individual_recs['rsi'] != 'N/A': match_count += 1
                        if individual_recs['bb'] == last_prev_bb_rec and individual_recs['bb'] != 'N/A': match_count += 1
                        metric_value = (match_count / 3) * 100 if match_count > 0 else 0
                        details = f"Rec. mantenida. Indicadores coincidentes: {match_count}/3."
                    else:
                        metric_type = 'Riesgo'
                        change_count = 0
                        if individual_recs['sma'] != last_prev_sma_rec and individual_recs['sma'] != 'N/A': change_count += 1
                        if individual_recs['rsi'] != last_prev_rsi_rec and individual_recs['rsi'] != 'N/A': change_count += 1
                        if individual_recs['bb'] != last_prev_bb_rec and individual_recs['bb'] != 'N/A': change_count += 1
                        metric_value = (change_count / 3) * 100 if change_count > 0 else 0
                        details = f"Rec. cambió de '{last_prev_rec}' a '{current_overall_rec}'. Indicadores cambiantes: {change_count}/3."
                else:
                    details = "Primera recomendación para el símbolo o datos insuficientes para comparar."

                with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as file:
                    writer = csv.writer(file)
                    writer.writerow([
                        now_dt.isoformat().replace('+00:00', 'Z'), # ISO format for JS
                        symbol,
                        current_overall_rec,
                        last_prev_rec,
                        metric_type,
                        round(metric_value, 2),
                        details
                    ])

                update_last_recommendation_file(symbol, now_dt.isoformat().replace('+00:00', 'Z'), current_overall_rec, individual_recs['sma'], individual_recs['rsi'], individual_recs['bb'], current_price)
                print(f"[{datetime.now().isoformat()}] Saved new entry for {symbol}: {current_overall_rec}, Price: {current_price}")
            else:
                print(f"[{datetime.now().isoformat()}] Skipping save for {symbol}: No significant change or time not passed.")

        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Error in scheduled analysis for {symbol}: {e}")

# --- API ROUTES ---

@app.route('/get_recommendations', methods=['GET'])
def get_recommendations():
    """
    API endpoint to retrieve historical recommendations with pagination and symbol filtering.
    """
    symbol_filter = request.args.get('symbol', default=None, type=str)
    page = request.args.get('page', default=1, type=int)
    limit = request.args.get('limit', default=20, type=int)

    recommendations = []
    current_time_utc = datetime.now(timezone.utc)
    threshold_time_utc = current_time_utc - timedelta(hours=24) # Use 24 hours for displayed history

    try:
        all_recommendations = []
        with open(CSV_FILE, mode='r', encoding='utf-8') as file:
            reader = csv.reader(file)
            header = next(reader, None) # Skip header

            for row in reader:
                if len(row) >= 7:
                    try:
                        timestamp_str, symbol, recommendation, prev_recommendation, metric_type, metric_value_str, details = row[:7]
                        entry_timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)

                        if (symbol_filter is None or symbol == symbol_filter) and entry_timestamp >= threshold_time_utc:
                            all_recommendations.append({
                                'timestamp': timestamp_str,
                                'symbol': symbol,
                                'recommendation': recommendation,
                                'prev_recommendation': prev_recommendation,
                                'metric_type': metric_type,
                                'metric_value': float(metric_value_str),
                                'details': details
                            })
                    except ValueError as ve:
                        print(f"Skipping malformed row (parsing error): {row} - {ve}")
                    except IndexError as ie:
                        print(f"Skipping malformed row (index error): {row} - {ie}")
                else:
                    print(f"Skipping malformed row (wrong length): {row}")

        all_recommendations.sort(key=lambda x: datetime.fromisoformat(x['timestamp'].replace('Z', '+00:00')).replace(tzinfo=timezone.utc), reverse=True)

        start_index = (page - 1) * limit
        end_index = start_index + limit
        paginated_recommendations = all_recommendations[start_index:end_index]

        total_pages = (len(all_recommendations) + limit - 1) // limit

        return jsonify({
            'recommendations': paginated_recommendations,
            'total_items': len(all_recommendations),
            'total_pages': total_pages,
            'current_page': page
        }), 200

    except FileNotFoundError:
        return jsonify({'recommendations': [], 'total_items': 0, 'total_pages': 0, 'current_page': page}), 200
    except Exception as e:
        print(f"Error getting recommendations: {e}")
        return jsonify({'message': f'Internal server error: {str(e)}'}), 500

@app.route('/get_available_symbols', methods=['GET'])
async def get_available_symbols():
    """API endpoint to get the list of available symbols dynamically from KuCoin."""
    try:
        symbols = await get_all_kucoin_symbols()
        return jsonify(symbols), 200
    except Exception as e:
        print(f"Error fetching available symbols: {e}")
        return jsonify({'message': f'Error fetching available symbols: {str(e)}'}), 500


@app.route('/get_latest_analysis/<symbol>', methods=['GET'])
async def get_latest_analysis(symbol):
    """
    API endpoint to get the latest analysis (klines and signals) for a specific symbol.
    Serves from cache if available, otherwise fetches live data.
    """
    print(f"[{datetime.now().isoformat()}] Frontend requested latest analysis for {symbol}")

    if symbol in current_analysis_cache and current_analysis_cache[symbol].get('klines'):
        print(f"[{datetime.now().isoformat()}] Serving from cache for {symbol}.")
        return jsonify(current_analysis_cache[symbol]), 200

    print(f"[{datetime.now().isoformat()}] Cache miss for {symbol}, trying to fetch live. (This should be rare if scheduler runs)")
    try:
        klines_data = await get_kucoin_klines(symbol)

        min_required_klines = max(20, 50, 14) + 1
        if not klines_data or len(klines_data) < min_required_klines:
            print(f"[{datetime.now().isoformat()}] Insufficient data for {symbol} on live fetch for frontend. Returning empty.")
            return jsonify({
                'overall_rec': 'hold', 'sma': 'N/A', 'rsi': 'N/A', 'bb': 'N/A',
                'klines': [], 'sma_short': [], 'sma_long': [], 'bb_bands': {'middle':[],'upper':[],'lower':[]}, 'rsi_data': []
            }), 200

        closing_prices = [p['y'] for p in klines_data]

        sma_short = calculate_sma(closing_prices, 20)
        sma_long = calculate_sma(closing_prices, 50)
        bollinger_bands = calculate_bollinger_bands(closing_prices, 20, 2)
        rsi = calculate_rsi(closing_prices, 14)
        combined_signals = get_combined_signals(sma_short, sma_long, rsi, bollinger_bands, closing_prices)

        response_data = {
            'overall_rec': combined_signals['overall'],
            'sma': combined_signals['sma'],
            'rsi': combined_signals['rsi'],
            'bb': combined_signals['bb'],
            'klines': klines_data,
            'sma_short': sma_short,
            'sma_long': sma_long,
            'bb_bands': bollinger_bands,
            'rsi_data': rsi
        }
        current_analysis_cache[symbol] = response_data

        return jsonify(response_data), 200
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error serving live analysis for {symbol}: {e}")
        return jsonify({'message': f'Error fetching live data: {str(e)}'}), 500

@app.route('/get_current_opportunities', methods=['GET'])
def get_current_opportunities():
    """
    API endpoint to get the current trading opportunities for all monitored symbols.
    Returns the latest overall recommendation from the cache.
    """
    opportunities = {'buy': [], 'sell': [], 'hold': [], 'no_data': []}
    for symbol, data in current_analysis_cache.items():
        if data and data.get('overall_rec'):
            if data['overall_rec'] == 'buy':
                opportunities['buy'].append(symbol)
            elif data['overall_rec'] == 'sell':
                opportunities['sell'].append(symbol)
            else:
                opportunities['hold'].append(symbol)
        else:
            opportunities['no_data'].append(symbol)
    return jsonify(opportunities), 200


# --- Task Scheduling Logic ---
scheduler = BackgroundScheduler()

# This function will be called once when the Flask app starts
def initial_setup():
    """
    Performs initial setup: starts the scheduler, fetches initial symbols,
    and schedules the main analysis job.
    """
    global SYMBOLS_TO_MONITOR # Declare as global to modify the list

    if not scheduler.running:
        scheduler.start()
        print("Scheduler started.")

    print("Running initial setup to populate cache and start analysis.")

    try:
        # Use a new event loop for this synchronous call if not already in an async context
        # This is crucial for calling async functions from a synchronous Flask startup
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If a loop is already running (e.g., in a development server with reloader),
            # we need to run the async task in a way that doesn't block the existing loop.
            # For simplicity in this context, we'll try to run it.
            # In production (gunicorn), a new loop is usually fine.
            print("Event loop is already running. Attempting to run initial symbol fetch.")
            # This might not be ideal for all WSGI servers, but works for basic cases.
            symbols_from_api = loop.run_until_complete(get_all_kucoin_symbols())
        else:
            # If no loop is running, create and run one.
            symbols_from_api = loop.run_until_complete(get_all_kucoin_symbols())

        SYMBOLS_TO_MONITOR.extend(symbols_from_api) # Populate the global list

        if not SYMBOLS_TO_MONITOR:
            print("[CRITICAL] No symbols loaded from KuCoin. Scheduled job will not run effectively.")
        else:
            print(f"[{datetime.now().isoformat()}] Initial SYMBOLS_TO_MONITOR populated with {len(SYMBOLS_TO_MONITOR)} symbols.")

            # Add the scheduled task AFTER SYMBOLS_TO_MONITOR is populated
            scheduler.add_job(
                lambda: asyncio.run(scheduled_analysis_job(SYMBOLS_TO_MONITOR)),
                'interval',
                minutes=2, # Run every 2 minutes
                id='full_crypto_analysis',
                max_instances=1 # Ensure only one instance runs at a time
            )

            # Execute the scheduled task at startup to populate the cache as soon as possible
            if SYMBOLS_TO_MONITOR:
                print(f"[{datetime.now().isoformat()}] Running initial scheduled analysis job.")
                loop.run_until_complete(scheduled_analysis_job(SYMBOLS_TO_MONITOR))

    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error during initial scheduler setup or symbol fetch: {e}")
        print("Scheduler might not be fully operational or symbols list is empty.")

# Call initial_setup when the Flask app starts
# This ensures it runs only once when the application is loaded
with app.app_context():
    initial_setup()

if __name__ == '__main__':
    print("Running Flask app in __main__ block (for local development).")
    # For local development, use_reloader=False is important to prevent
    # the scheduler from being initialized multiple times.
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
