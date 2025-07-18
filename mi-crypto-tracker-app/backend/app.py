from flask import Flask, request, jsonify
from flask_cors import CORS
import csv
import os
from datetime import datetime, timedelta, timezone 

app = Flask(__name__)
CORS(app)

CSV_FILE = 'data.csv' 
HISTORY_DURATION_HOURS = 24 

# Archivo adicional para guardar la ÚLTIMA recomendación por símbolo
# AÑADIDO 'last_price' para el seguimiento del porcentaje de cambio.
LAST_REC_FILE = 'last_recommendations.csv'

def ensure_csv_exists():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(['timestamp', 'symbol', 'recommendation', 'prev_recommendation', 'metric_type', 'metric_value', 'details'])
    
    # AÑADIDO 'last_price' al encabezado de last_recommendations.csv
    if not os.path.exists(LAST_REC_FILE):
        with open(LAST_REC_FILE, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(['symbol', 'timestamp', 'recommendation', 'sma_rec', 'rsi_rec', 'bb_rec', 'last_price'])

ensure_csv_exists()

@app.route('/save_recommendation', methods=['POST'])
def save_recommendation():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'message': 'No data provided'}), 400

        symbol = data.get('symbol')
        recommendation = data.get('recommendation')
        timestamp_iso = data.get('timestamp')
        
        sma_rec = data.get('sma_rec', 'N/A') 
        rsi_rec = data.get('rsi_rec', 'N/A')
        bb_rec = data.get('bb_rec', 'N/A')
        current_price = data.get('current_price') # NUEVO: Recibe el precio actual del frontend

        current_dt = datetime.fromisoformat(timestamp_iso.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)

        # 1. Obtener la última recomendación guardada para este símbolo
        last_rec_data = None
        current_rows_last_rec = []
        if os.path.exists(LAST_REC_FILE):
            with open(LAST_REC_FILE, mode='r', newline='', encoding='utf-8') as file:
                reader = csv.DictReader(file)
                current_rows_last_rec = list(reader)

            for row in current_rows_last_rec:
                if row['symbol'] == symbol:
                    last_rec_data = row
                    break
        
        prev_recommendation = last_rec_data.get('recommendation', 'N/A') if last_rec_data else 'N/A'
        prev_sma_rec = last_rec_data.get('sma_rec', 'N/A') if last_rec_data else 'N/A'
        prev_rsi_rec = last_rec_data.get('rsi_rec', 'N/A') if last_rec_data else 'N/A'
        prev_bb_rec = last_rec_data.get('bb_rec', 'N/A') if last_rec_data else 'N/A'
        # prev_price = float(last_rec_data.get('last_price', 0.0)) if last_rec_data and last_rec_data.get('last_price') else 0.0 # Aunque no lo usemos aquí, el frontend lo necesitará


        metric_type = 'N/A'
        metric_value = 0.0
        details = ""

        # Lógica de cálculo de acierto/riesgo (no cambia la lógica, solo el punto de decisión del guardado)
        if prev_recommendation != 'N/A' and recommendation != 'N/A':
            if recommendation == prev_recommendation:
                metric_type = 'Acierto'
                match_count = 0
                if sma_rec == prev_sma_rec and sma_rec != 'N/A': match_count += 1
                if rsi_rec == prev_rsi_rec and rsi_rec != 'N/A': match_count += 1
                if bb_rec == prev_bb_rec and bb_rec != 'N/A': match_count += 1
                
                metric_value = (match_count / 3) * 100 if match_count > 0 else 0
                details = f"Rec. mantenida. Indicadores coincidentes: {match_count}/3."
            else:
                metric_type = 'Riesgo'
                change_count = 0
                if sma_rec != prev_sma_rec and sma_rec != 'N/A': change_count += 1
                if rsi_rec != prev_rsi_rec and rsi_rec != 'N/A': change_count += 1
                if bb_rec != prev_bb_rec and bb_rec != 'N/A': change_count += 1
                
                metric_value = (change_count / 3) * 100 if change_count > 0 else 0
                details = f"Rec. cambió de '{prev_recommendation}' a '{recommendation}'. Indicadores cambiantes: {change_count}/3."
        else:
            details = "Primera recomendación para el símbolo o datos insuficientes para comparar."

        # 2. Guardar la entrada en el historial general (data.csv)
        with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow([
                timestamp_iso,
                symbol,
                recommendation,
                prev_recommendation, 
                metric_type,
                round(metric_value, 2), # Redondea el porcentaje
                details
            ])
        
        # 3. Actualizar la última recomendación conocida para este símbolo (last_recommendations.csv)
        found = False
        updated_rows = []
        
        if current_rows_last_rec:
            for row in current_rows_last_rec:
                if row['symbol'] == symbol:
                    updated_rows.append({
                        'symbol': symbol,
                        'timestamp': timestamp_iso,
                        'recommendation': recommendation,
                        'sma_rec': sma_rec,
                        'rsi_rec': rsi_rec,
                        'bb_rec': bb_rec,
                        'last_price': current_price # NUEVO: Guardar el precio actual
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
                'last_price': current_price # NUEVO: Guardar el precio actual
            })
        
        with open(LAST_REC_FILE, mode='w', newline='', encoding='utf-8') as file:
            # Asegúrate de que fieldnames incluya 'last_price'
            writer = csv.DictWriter(file, fieldnames=['symbol', 'timestamp', 'recommendation', 'sma_rec', 'rsi_rec', 'bb_rec', 'last_price'])
            writer.writeheader() 
            writer.writerows(updated_rows) 

        print(f"[{datetime.now().isoformat()}] Saved and updated last rec for {symbol}: {recommendation}, Price: {current_price}")
        return jsonify({'message': 'Recommendation saved and updated successfully'}), 200

    except Exception as e:
        print(f"Error saving recommendation: {e}")
        return jsonify({'message': f'Internal server error: {str(e)}'}), 500

# Endpoint para obtener las recomendaciones (sin cambios en la lógica de obtención)
@app.route('/get_recommendations', methods=['GET'])
def get_recommendations():
    recommendations = []
    
    current_time_utc = datetime.now(timezone.utc) 
    threshold_time_utc = current_time_utc - timedelta(hours=HISTORY_DURATION_HOURS)

    try:
        with open(CSV_FILE, mode='r', encoding='utf-8') as file:
            reader = csv.reader(file)
            header = next(reader, None) 

            for row in reader:
                if len(row) >= 7: 
                    try:
                        timestamp_str, symbol, recommendation, prev_recommendation, metric_type, metric_value_str, details = row[:7]
                        entry_timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
                        
                        if entry_timestamp >= threshold_time_utc:
                            recommendations.append({
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

        recommendations.sort(key=lambda x: datetime.fromisoformat(x['timestamp'].replace('Z', '+00:00')).replace(tzinfo=timezone.utc), reverse=True)
        
        return jsonify(recommendations), 200

    except FileNotFoundError:
        return jsonify([]), 200
    except Exception as e:
        print(f"Error getting recommendations: {e}")
        return jsonify({'message': f'Internal server error: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
