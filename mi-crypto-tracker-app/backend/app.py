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
# Configuración explícita de CORS para permitir todas las solicitudes de origen
CORS(app, resources={r"/*": {"origins": "*"}})

# --- BACKEND CONFIGURATION ---
KUCOIN_INTERVAL = "1hour"
KUCOIN_LIMIT = 200

SAVE_REC_TO_BACKEND_INTERVAL = timedelta(hours=1)
PRICE_CHANGE_THRESHOLD = 0.03

CSV_FILE = 'data.csv'
LAST_REC_FILE = 'last_recommendations.csv'

current_analysis_cache = {}

SYMBOLS_TO_MONITOR = []

# --- CONSTANTES PARA NUEVOS INDICADORES Y GESTIÓN DE RIESGOS ---
# Parámetros para MACD
MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9

# Parámetros para Oscilador Estocástico
STOCH_K_PERIOD = 14
STOCH_D_PERIOD = 3
STOCH_OVERBOUGHT = 80
STOCH_OVERSOLD = 20

# Parámetros para Stop-Loss y Take-Profit
STOP_LOSS_PERCENTAGE = 0.02 # 2% por debajo del precio actual
TAKE_PROFIT_PERCENTAGE = 0.04 # 4% por encima del precio actual


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
            writer.writerow(['symbol', 'timestamp', 'recommendation', 'sma_rec', 'rsi_rec', 'bb_rec', 'macd_rec', 'stoch_rec', 'last_price'])

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

def update_last_recommendation_file(symbol, timestamp_iso, recommendation, sma_rec, rsi_rec, bb_rec, macd_rec, stoch_rec, current_price):
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
                'macd_rec': macd_rec, # Nuevo
                'stoch_rec': stoch_rec, # Nuevo
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
            'macd_rec': macd_rec, # Nuevo
            'stoch_rec': stoch_rec, # Nuevo
            'last_price': current_price
        })

    with open(LAST_REC_FILE, mode='w', newline='', encoding='utf-8') as file:
        # Actualizar fieldnames para incluir los nuevos indicadores
        fieldnames=['symbol', 'timestamp', 'recommendation', 'sma_rec', 'rsi_rec', 'bb_rec', 'macd_rec', 'stoch_rec', 'last_price']
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updated_rows)

# --- NEW FUNCTION: Get ALL KuCoin Tickers (for volume data) ---
async def get_all_kucoin_tickers():
    """Fetches all market tickers from the KuCoin API to get volume data."""
    url = "https://api.kucoin.com/api/v1/market/allTickers"
    print(f"[{datetime.now().isoformat()}] Fetching all tickers from KuCoin API: {url}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=15.0)
            response.raise_for_status()

            data = response.json()
            if not data or not data.get('data') or not data['data'].get('ticker') or not isinstance(data['data']['ticker'], list):
                raise ValueError("KuCoin API for tickers returned invalid or no data.")
            
            # Create a dictionary for quick lookup by symbol
            tickers_map = {ticker['symbol']: ticker for ticker in data['data']['ticker']}
            print(f"[{datetime.now().isoformat()}] Fetched {len(tickers_map)} tickers from KuCoin.")
            return tickers_map

    except httpx.HTTPStatusError as e:
        print(f"HTTP Error fetching tickers from KuCoin: {e.response.status_code} - {e.response.text}")
        return {}
    except httpx.RequestError as e:
        print(f"Network error fetching tickers from KuCoin: {e}")
        return {}
    except ValueError as e:
        print(f"KuCoin data error for tickers: {e}")
        return {}
    except Exception as e:
        print(f"Unexpected error fetching tickers from KuCoin: {e}")
        return {}

# --- NEW FUNCTION: Get ALL KuCoin Symbols (now with volume sorting) ---
async def get_all_kucoin_symbols():
    """
    Fetches all tradable symbols from the KuCoin API, filters for USDT/USDC pairs,
    sorts by 24h transaction volume, and returns the top 20 symbols.
    """
    url_symbols = "https://api.kucoin.com/api/v1/symbols"
    print(f"[{datetime.now().isoformat()}] Fetching all symbols from KuCoin API: {url_symbols}")
    
    try:
        async with httpx.AsyncClient() as client:
            response_symbols = await client.get(url_symbols, timeout=15.0)
            response_symbols.raise_for_status()
            data_symbols = response_symbols.json()

            if not data_symbols or not data_symbols.get('data') or not isinstance(data_symbols['data'], list):
                raise ValueError("KuCoin API for symbols returned invalid or no data.")
            
            # Fetch all tickers for volume data
            tickers_map = await get_all_kucoin_tickers()

            tradable_pairs_with_volume = []
            for item in data_symbols['data']:
                symbol_name = f"{item['baseCurrency']}-{item['quoteCurrency']}"
                if item.get('enableTrading') and item['quoteCurrency'] in ['USDT', 'USDC']:
                    ticker_info = tickers_map.get(symbol_name)
                    if ticker_info and ticker_info.get('volValue'): # Check if ticker data and volume exist
                        try:
                            volume = float(ticker_info['volValue'])
                            tradable_pairs_with_volume.append({'symbol': symbol_name, 'volume': volume})
                        except ValueError:
                            print(f"[{datetime.now().isoformat()}] Could not parse volume for {symbol_name}: {ticker_info['volValue']}")
                            continue # Skip if volume cannot be parsed
            
            # Sort by volume in descending order
            tradable_pairs_with_volume.sort(key=lambda x: x['volume'], reverse=True)
            
            # Extract only the symbol names and limit to top 20
            top_20_symbols = [pair['symbol'] for pair in tradable_pairs_with_volume[:20]]

            print(f"[{datetime.now().isoformat()}] Fetched {len(tradable_pairs_with_volume)} tradable symbols with volume data.")
            print(f"[{datetime.now().isoformat()}] Returning top 20 symbols by volume: {top_20_symbols}")
            return top_20_symbols

    except httpx.HTTPStatusError as e:
        print(f"HTTP Error fetching symbols/tickers from KuCoin: {e.response.status_code} - {e.response.text}")
        return []
    except httpx.RequestError as e:
        print(f"Network error fetching symbols/tickers from KuCoin: {e}")
        return []
    except ValueError as e:
        print(f"KuCoin data error for symbols/tickers: {e}")
        return []
    except Exception as e:
        print(f"Unexpected error fetching symbols/tickers from KuCoin: {e}")
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


# --- INDICATOR CALCULATION FUNCTIONS ---
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

def calculate_ema(data, period):
    """Calculates Exponential Moving Average (EMA) for given data."""
    if not data or len(data) < period:
        return [None] * len(data) if data else []

    ema_values = []
    smoothing_factor = 2 / (period + 1)

    # Calculate initial SMA for the first EMA point
    initial_sma = sum(data[0:period]) / period
    ema_values.append(initial_sma)

    for i in range(period, len(data)):
        ema = (data[i] - ema_values[-1]) * smoothing_factor + ema_values[-1]
        ema_values.append(ema)
    
    # Pad with None for initial points
    return [None] * (period - 1) + [{'y': val} for val in ema_values]


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

    for _ in range(period):
        rsi_values.append(None)

    gains = []
    losses = []
    for i in range(1, len(data)):
        diff = data[i] - data[i - 1]
        gains.append(diff if diff > 0 else 0)
        losses.append(abs(diff) if diff < 0 else 0)

    avg_gain = sum(gains[0:period]) / period
    avg_loss = sum(losses[0:period]) / period

    if avg_loss == 0:
        rsi_values.append({'y': 100.0})
    else:
        rs = avg_gain / avg_loss
        rsi_values.append({'y': 100 - (100 / (1 + rs))})

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

def calculate_macd(data, fast_period, slow_period, signal_period):
    """Calculates Moving Average Convergence Divergence (MACD) for given data."""
    if not data or len(data) < max(fast_period, slow_period) + signal_period:
        return {'macd_line': [None]*len(data), 'signal_line': [None]*len(data), 'histogram': [None]*len(data)}

    ema_fast = [v['y'] for v in calculate_ema(data, fast_period)]
    ema_slow = [v['y'] for v in calculate_ema(data, slow_period)]

    macd_line = []
    for i in range(len(data)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line.append(ema_fast[i] - ema_slow[i])
        else:
            macd_line.append(None)

    # Calculate signal line (EMA of MACD line)
    signal_line_raw = calculate_ema([m for m in macd_line if m is not None], signal_period)
    signal_line = [None] * (len(macd_line) - len(signal_line_raw)) + [s['y'] for s in signal_line_raw]

    histogram = []
    for i in range(len(macd_line)):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram.append(macd_line[i] - signal_line[i])
        else:
            histogram.append(None)
    
    # Format for Chart.js
    macd_line_formatted = [{'y': val} if val is not None else None for val in macd_line]
    signal_line_formatted = [{'y': val} if val is not None else None for val in signal_line]
    histogram_formatted = [{'y': val} if val is not None else None for val in histogram]

    return {'macd_line': macd_line_formatted, 'signal_line': signal_line_formatted, 'histogram': histogram_formatted}


def calculate_stochastic_oscillator(data, k_period, d_period):
    """Calculates Stochastic Oscillator (%K and %D) for given data."""
    if not data or len(data) < k_period:
        return {'k_line': [None]*len(data), 'd_line': [None]*len(data)}

    k_line = []
    for i in range(len(data)):
        if i < k_period - 1:
            k_line.append(None)
        else:
            period_low = min(data[i - k_period + 1 : i + 1])
            period_high = max(data[i - k_period + 1 : i + 1])
            current_close = data[i]

            if (period_high - period_low) != 0:
                k = ((current_close - period_low) / (period_high - period_low)) * 100
            else:
                k = 50 # Avoid division by zero, neutral value
            k_line.append(k)
    
    # Calculate %D line (SMA of %K line)
    d_line_raw = calculate_sma([k for k in k_line if k is not None], d_period)
    d_line = [None] * (len(k_line) - len(d_line_raw)) + [d['y'] for d in d_line_raw]

    # Format for Chart.js
    k_line_formatted = [{'y': val} if val is not None else None for val in k_line]
    d_line_formatted = [{'y': val} if val is not None else None for val in d_line]

    return {'k_line': k_line_formatted, 'd_line': d_line_formatted}


# --- Combined Signals Logic ---
def get_combined_signals(sma_short, sma_long, rsi, bollinger_bands, macd_data, stoch_data, closing_prices):
    """Determines combined trading signals based on SMA, RSI, Bollinger Bands, MACD, and Stochastic Oscillator."""
    # Inicializar recomendaciones individuales
    sma_rec = 'hold'
    rsi_rec = 'hold'
    bb_rec = 'hold'
    macd_rec = 'hold'
    stoch_rec = 'hold'

    # Parámetros de los indicadores (los mismos que en scheduled_analysis_job)
    SMA_SHORT_PERIOD = 10
    SMA_LONG_PERIOD = 30
    RSI_OVERBOUGHT = 65
    RSI_OVERSOLD = 35
    BB_PERIOD = 15
    STOCH_OVERBOUGHT = 80
    STOCH_OVERSOLD = 20

    # Lógica para SMA
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

    # Lógica para RSI
    valid_rsi = [v['y'] for v in rsi if v is not None]
    if len(valid_rsi) > 0:
        last_rsi = valid_rsi[-1]
        if last_rsi > RSI_OVERBOUGHT:
            rsi_rec = 'sell'
        elif last_rsi < RSI_OVERSOLD:
            rsi_rec = 'buy'
    else:
        rsi_rec = 'N/A'

    # Lógica para Bandas de Bollinger
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

    # Lógica para MACD
    valid_macd_line = [v['y'] for v in macd_data['macd_line'] if v is not None]
    valid_signal_line = [v['y'] for v in macd_data['signal_line'] if v is not None]
    if len(valid_macd_line) >= 2 and len(valid_signal_line) >= 2:
        last_macd = valid_macd_line[-1]
        prev_macd = valid_macd_line[-2]
        last_signal = valid_signal_line[-1]
        prev_signal = valid_signal_line[-2]

        # Cruce alcista: MACD cruza por encima de la línea de señal
        if prev_macd <= prev_signal and last_macd > last_signal:
            macd_rec = 'buy'
        # Cruce bajista: MACD cruza por debajo de la línea de señal
        elif prev_macd >= prev_signal and last_macd < last_signal:
            macd_rec = 'sell'
    else:
        macd_rec = 'N/A'

    # Lógica para Oscilador Estocástico
    valid_k_line = [v['y'] for v in stoch_data['k_line'] if v is not None]
    valid_d_line = [v['y'] for v in stoch_data['d_line'] if v is not None]
    if len(valid_k_line) > 0 and len(valid_d_line) > 0:
        last_k = valid_k_line[-1]
        last_d = valid_d_line[-1]

        # Cruce alcista y sobreventa
        if last_k > last_d and last_k < STOCH_OVERSOLD:
            stoch_rec = 'buy'
        # Cruce bajista y sobrecompra
        elif last_k < last_d and last_k > STOCH_OVERBOUGHT:
            stoch_rec = 'sell'
        # Cruce alcista general (menos fuerte)
        elif last_k > last_d and valid_k_line[-2] <= valid_d_line[-2]:
            stoch_rec = 'buy'
        # Cruce bajista general (menos fuerte)
        elif last_k < last_d and valid_k_line[-2] >= valid_d_line[-2]:
            stoch_rec = 'sell'
    else:
        stoch_rec = 'N/A'


    # Conteo de señales
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
    
    if macd_rec == 'buy': buy_count += 1 # Nuevo
    elif macd_rec == 'sell': sell_count += 1 # Nuevo
    else: na_count += 1 # Nuevo

    if stoch_rec == 'buy': buy_count += 1 # Nuevo
    elif stoch_rec == 'sell': sell_count += 1 # Nuevo
    else: na_count += 1 # Nuevo

    overall_recommendation = 'hold'

    # Lógica de combinación de señales más relajada (mayoría simple)
    if buy_count > sell_count and buy_count >= 1: # Si hay más señales de compra y al menos una
        overall_recommendation = 'buy'
    elif sell_count > buy_count and sell_count >= 1: # Si hay más señales de venta y al menos una
        overall_recommendation = 'sell'
    else: # En caso de empate, o solo "hold" / "N/A"
        overall_recommendation = 'hold'

    # Se devuelven los conteos para fines de diagnóstico
    return {'sma': sma_rec, 'rsi': rsi_rec, 'bb': bb_rec, 'macd': macd_rec, 'stoch': stoch_rec, # Añadidos
            'overall': overall_recommendation,
            'buy_count': buy_count, 'sell_count': sell_count, 'na_count': na_count}


# --- SCHEDULED TASK TO FETCH AND ANALYZE DATA ---
async def scheduled_analysis_job(symbols):
    """
    Scheduled job to fetch klines, calculate indicators, and determine trading signals
    for a list of symbols. Results are cached and saved to CSV if conditions are met.
    """
    print(f"[{datetime.now().isoformat()}] Scheduled job started for {len(symbols)} symbols.")
    
    # Definir los periodos de los indicadores aquí para usarlos en min_required_klines
    SMA_SHORT_PERIOD = 10 
    SMA_LONG_PERIOD = 30  
    RSI_PERIOD = 14
    BB_PERIOD = 15        
    # Nuevos periodos para MACD y Stochastic
    MACD_MAX_PERIOD = max(MACD_FAST_PERIOD, MACD_SLOW_PERIOD) + MACD_SIGNAL_PERIOD
    STOCH_MAX_PERIOD = STOCH_K_PERIOD + STOCH_D_PERIOD

    for symbol in symbols:
        try:
            print(f"[{datetime.now().isoformat()}] Analyzing {symbol}...")
            klines_data = await get_kucoin_klines(symbol)

            # Define la cantidad mínima de velas requeridas para los indicadores
            # Se usa el máximo de todos los periodos para asegurar que haya suficientes puntos para el cálculo inicial.
            min_required_klines = max(SMA_SHORT_PERIOD, SMA_LONG_PERIOD, RSI_PERIOD, BB_PERIOD, MACD_MAX_PERIOD, STOCH_MAX_PERIOD) + 1 
            
            if not klines_data or len(klines_data) < min_required_klines:
                print(f"  [{datetime.now().isoformat()}] {symbol}: Datos insuficientes. Velas obtenidas: {len(klines_data) if klines_data else 0}, Requeridas: {min_required_klines}. Saltando análisis detallado.")
                current_overall_rec = 'hold'
                individual_recs = {'sma': 'N/A', 'rsi': 'N/A', 'bb': 'N/A', 'macd': 'N/A', 'stoch': 'N/A'} # Actualizado
                current_price = klines_data[-1]['y'] if klines_data and len(klines_data) > 0 else 0.0
                
                # Log detallado para casos de datos insuficientes
                print(f"  [{datetime.now().isoformat()}] {symbol}: Señales Individuales: SMA: {individual_recs['sma']}, RSI: {individual_recs['rsi']}, BB: {individual_recs['bb']}, MACD: {individual_recs['macd']}, Stoch: {individual_recs['stoch']}. Recomendación General: {current_overall_rec}")

            else:
                closing_prices = [p['y'] for p in klines_data]
                current_price = closing_prices[-1]

                # Se pasan los periodos actualizados a las funciones de cálculo
                sma_short = calculate_sma(closing_prices, SMA_SHORT_PERIOD)
                sma_long = calculate_sma(closing_prices, SMA_LONG_PERIOD)
                bollinger_bands = calculate_bollinger_bands(closing_prices, BB_PERIOD, 2) # std_dev_multiplier se mantiene en 2
                rsi = calculate_rsi(closing_prices, RSI_PERIOD)
                macd_data = calculate_macd(closing_prices, MACD_FAST_PERIOD, MACD_SLOW_PERIOD, MACD_SIGNAL_PERIOD) # Nuevo
                stoch_data = calculate_stochastic_oscillator(closing_prices, STOCH_K_PERIOD, STOCH_D_PERIOD) # Nuevo

                combined_signals = get_combined_signals(sma_short, sma_long, rsi, bollinger_bands, macd_data, stoch_data, closing_prices) # Actualizado
                current_overall_rec = combined_signals['overall']
                # Actualizar individual_recs para incluir los nuevos indicadores
                individual_recs = {'sma': combined_signals['sma'], 'rsi': combined_signals['rsi'], 'bb': combined_signals['bb'],
                                   'macd': combined_signals['macd'], 'stoch': combined_signals['stoch']}
                
                # --- Gestión de Riesgos: Cálculo Básico de Stop-Loss y Take-Profit ---
                # Estos son solo niveles sugeridos, no se ejecutan automáticamente.
                stop_loss_price = round(current_price * (1 - STOP_LOSS_PERCENTAGE), 2)
                take_profit_price = round(current_price * (1 + TAKE_PROFIT_PERCENTAGE), 2)

                # Log detallado para el análisis completo
                print(f"  [{datetime.now().isoformat()}] {symbol}: Velas obtenidas: {len(klines_data)}. Señales Individuales: SMA: {individual_recs['sma']}, RSI: {individual_recs['rsi']}, BB: {individual_recs['bb']}, MACD: {individual_recs['macd']}, Stoch: {individual_recs['stoch']}. Conteo: Compra: {combined_signals['buy_count']}, Venta: {combined_signals['sell_count']}, N/A: {combined_signals['na_count']}. Recomendación General: {current_overall_rec}. SL: {stop_loss_price}, TP: {take_profit_price}")


            # Update cache with full results for this symbol
            current_analysis_cache[symbol] = {
                'overall_rec': current_overall_rec,
                'sma': individual_recs['sma'],
                'rsi': individual_recs['rsi'],
                'bb': individual_recs['bb'],
                'macd': individual_recs['macd'], # Nuevo
                'stoch': individual_recs['stoch'], # Nuevo
                'klines': klines_data,
                'sma_short': sma_short,
                'sma_long': sma_long,
                'bb_bands': bollinger_bands,
                'rsi_data': rsi,
                'macd_data': macd_data, # Nuevo
                'stoch_data': stoch_data, # Nuevo
                'stop_loss_price': stop_loss_price, # Nuevo
                'take_profit_price': take_profit_price # Nuevo
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
                    print(f"  [{datetime.now().isoformat()}] {symbol}: % cambio precio: {(percentage_change*100):.2f}% (Umbral: {PRICE_CHANGE_THRESHOLD*100:.0f}%), Tiempo pasado: {has_time_passed}")
                else:
                     has_significant_price_change = True # If no previous price, save it

                if has_time_passed or has_significant_price_change:
                    should_save = True
            else: # First recommendation for this symbol
                should_save = True
                print(f"  [{datetime.now().isoformat()}] {symbol}: Primera recomendación, guardando.")

            if should_save:
                last_prev_rec = last_rec_info.get('recommendation', 'N/A') if last_rec_info else 'N/A'
                last_prev_sma_rec = last_rec_info.get('sma_rec', 'N/A') if last_rec_info else 'N/A'
                last_prev_rsi_rec = last_rec_info.get('rsi_rec', 'N/A') if last_rec_info else 'N/A'
                last_prev_bb_rec = last_rec_info.get('bb_rec', 'N/A') if last_rec_info else 'N/A'
                last_prev_macd_rec = last_rec_info.get('macd_rec', 'N/A') if last_rec_info else 'N/A' # Nuevo
                last_prev_stoch_rec = last_rec_info.get('stoch_rec', 'N/A') if last_rec_info else 'N/A' # Nuevo

                metric_type = 'N/A'
                metric_value = 0.0
                details = ""

                # Ajustar la lógica de comparación para 5 indicadores
                if last_prev_rec != 'N/A' and current_overall_rec != 'N/A':
                    if current_overall_rec == last_prev_rec:
                        metric_type = 'Acierto'
                        match_count = 0
                        if individual_recs['sma'] == last_prev_sma_rec and individual_recs['sma'] != 'N/A': match_count += 1
                        if individual_recs['rsi'] == last_prev_rsi_rec and individual_recs['rsi'] != 'N/A': match_count += 1
                        if individual_recs['bb'] == last_prev_bb_rec and individual_recs['bb'] != 'N/A': match_count += 1
                        if individual_recs['macd'] == last_prev_macd_rec and individual_recs['macd'] != 'N/A': match_count += 1 # Nuevo
                        if individual_recs['stoch'] == last_prev_stoch_rec and individual_recs['stoch'] != 'N/A': match_count += 1 # Nuevo
                        metric_value = (match_count / 5) * 100 if match_count > 0 else 0 # Dividir por 5
                        details = f"Rec. mantenida. Indicadores coincidentes: {match_count}/5."
                    else:
                        metric_type = 'Riesgo'
                        change_count = 0
                        if individual_recs['sma'] != last_prev_sma_rec and individual_recs['sma'] != 'N/A': change_count += 1
                        if individual_recs['rsi'] != last_prev_rsi_rec and individual_recs['rsi'] != 'N/A': change_count += 1
                        if individual_recs['bb'] != last_prev_bb_rec and individual_recs['bb'] != 'N/A': change_count += 1
                        if individual_recs['macd'] != last_prev_macd_rec and individual_recs['macd'] != 'N/A': change_count += 1 # Nuevo
                        if individual_recs['stoch'] != last_prev_stoch_rec and individual_recs['stoch'] != 'N/A': change_count += 1 # Nuevo
                        metric_value = (change_count / 5) * 100 if change_count > 0 else 0 # Dividir por 5
                        details = f"Rec. cambió de '{last_prev_rec}' a '{current_overall_rec}'. Indicadores cambiantes: {change_count}/5."
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

                # Actualizar update_last_recommendation_file con los nuevos indicadores
                update_last_recommendation_file(symbol, now_dt.isoformat().replace('+00:00', 'Z'), current_overall_rec, individual_recs['sma'], individual_recs['rsi'], individual_recs['bb'], individual_recs['macd'], individual_recs['stoch'], current_price)
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
                # Asegurarse de que la fila tenga al menos 7 elementos (los originales)
                if len(row) >= 7:
                    try:
                        # Extraer los primeros 7 elementos para compatibilidad con el formato anterior
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

        # Definir los periodos de los indicadores aquí para usarlos en min_required_klines
        SMA_SHORT_PERIOD = 10 
        SMA_LONG_PERIOD = 30  
        RSI_PERIOD = 14
        BB_PERIOD = 15        
        MACD_MAX_PERIOD = max(MACD_FAST_PERIOD, MACD_SLOW_PERIOD) + MACD_SIGNAL_PERIOD
        STOCH_MAX_PERIOD = STOCH_K_PERIOD + STOCH_D_PERIOD

        min_required_klines = max(SMA_SHORT_PERIOD, SMA_LONG_PERIOD, RSI_PERIOD, BB_PERIOD, MACD_MAX_PERIOD, STOCH_MAX_PERIOD) + 1
        if not klines_data or len(klines_data) < min_required_klines:
            print(f"[{datetime.now().isoformat()}] Insufficient data for {symbol} on live fetch for frontend. Returning empty.")
            return jsonify({
                'overall_rec': 'hold', 'sma': 'N/A', 'rsi': 'N/A', 'bb': 'N/A', 'macd': 'N/A', 'stoch': 'N/A',
                'klines': [], 'sma_short': [], 'sma_long': [], 'bb_bands': {'middle':[],'upper':[],'lower':[]}, 'rsi_data': [],
                'macd_data': {'macd_line': [], 'signal_line': [], 'histogram': []}, 'stoch_data': {'k_line': [], 'd_line': []},
                'stop_loss_price': None, 'take_profit_price': None
            }), 200

        closing_prices = [p['y'] for p in klines_data]

        # Se pasan los periodos actualizados a las funciones de cálculo
        sma_short = calculate_sma(closing_prices, SMA_SHORT_PERIOD)
        sma_long = calculate_sma(closing_prices, SMA_LONG_PERIOD)
        bollinger_bands = calculate_bollinger_bands(closing_prices, BB_PERIOD, 2)
        rsi = calculate_rsi(closing_prices, RSI_PERIOD)
        macd_data = calculate_macd(closing_prices, MACD_FAST_PERIOD, MACD_SLOW_PERIOD, MACD_SIGNAL_PERIOD)
        stoch_data = calculate_stochastic_oscillator(closing_prices, STOCH_K_PERIOD, STOCH_D_PERIOD)

        combined_signals = get_combined_signals(sma_short, sma_long, rsi, bollinger_bands, macd_data, stoch_data, closing_prices)

        current_price = closing_prices[-1] if closing_prices else 0.0
        stop_loss_price = round(current_price * (1 - STOP_LOSS_PERCENTAGE), 2)
        take_profit_price = round(current_price * (1 + TAKE_PROFIT_PERCENTAGE), 2)

        response_data = {
            'overall_rec': combined_signals['overall'],
            'sma': combined_signals['sma'],
            'rsi': combined_signals['rsi'],
            'bb': combined_signals['bb'],
            'macd': combined_signals['macd'],
            'stoch': combined_signals['stoch'],
            'klines': klines_data,
            'sma_short': sma_short,
            'sma_long': sma_long,
            'bb_bands': bollinger_bands,
            'rsi_data': rsi,
            'macd_data': macd_data,
            'stoch_data': stoch_data,
            'stop_loss_price': stop_loss_price,
            'take_profit_price': take_profit_price
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
