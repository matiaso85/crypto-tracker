from flask import Flask, request, jsonify
from flask_cors import CORS
import csv
import os
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
CORS(app)

CSV_FILE = 'data.csv'
# La duración del historial ahora también se aplica al historial para el cálculo de acierto.
HISTORY_DURATION_HOURS = 24 

# Archivo adicional para guardar la ÚLTIMA recomendación por símbolo
# Esto es crucial para comparar la recomendación actual con la previa.
LAST_REC_FILE = 'last_recommendations.csv'

def ensure_csv_exists():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            # NUEVOS ENCABEZADOS para el historial: incluir tipo de entrada y detalles
            writer.writerow(['timestamp', 'symbol', 'recommendation', 'prev_recommendation', 'metric_type', 'metric_value', 'details'])
    
    if not os.path.exists(LAST_REC_FILE):
        with open(LAST_REC_FILE, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(['symbol', 'timestamp', 'recommendation', 'sma_rec', 'rsi_rec', 'bb_rec'])

# Asegurar que ambos CSV existan al inicio de la aplicación
ensure_csv_exists()

# Endpoint para guardar una recomendación
@app.route('/save_recommendation', methods=['POST'])
def save_recommendation():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'message': 'No data provided'}), 400

        symbol = data.get('symbol')
        recommendation = data.get('recommendation')
        timestamp_iso = data.get('timestamp')
        
        # NUEVOS DATOS: Recs individuales y detalles para cálculo de métricas
        sma_rec = data.get('sma_rec')
        rsi_rec = data.get('rsi_rec')
        bb_rec = data.get('bb_rec')
        
        # Convierte el timestamp a un objeto datetime UTC
        current_dt = datetime.fromisoformat(timestamp_iso.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)

        # 1. Obtener la última recomendación guardada para este símbolo
        last_rec_data = {}
        if os.path.exists(LAST_REC_FILE):
            with open(LAST_REC_FILE, mode='r', newline='', encoding='utf-8') as file:
                reader = csv.DictReader(file) # Usar DictReader para acceder por nombre de columna
                for row in reader:
                    if row['symbol'] == symbol:
                        last_rec_data = row
                        break # Asume que solo hay una última recomendación por símbolo
        
        prev_recommendation = last_rec_data.get('recommendation', 'N/A')
        prev_sma_rec = last_rec_data.get('sma_rec', 'N/A')
        prev_rsi_rec = last_rec_data.get('rsi_rec', 'N/A')
        prev_bb_rec = last_rec_data.get('bb_rec', 'N/A')

        metric_type = 'N/A'
        metric_value = 0.0
        details = ""

        # Lógica de cálculo de acierto/riesgo
        if prev_recommendation != 'N/A': # Solo si hay una recomendación previa
            if recommendation == prev_recommendation:
                metric_type = 'Acierto'
                # Calcular porcentaje de acierto basado en cuántos indicadores se mantuvieron
                match_count = 0
                if sma_rec == prev_sma_rec: match_count += 1
                if rsi_rec == prev_rsi_rec: match_count += 1
                if bb_rec == prev_bb_rec: match_count += 1
                metric_value = (match_count / 3) * 100 if match_count > 0 else 0
                details = f"Recomendación mantenida. Indicadores coincidentes: {match_count}/3."
            else:
                metric_type = 'Riesgo'
                # Calcular porcentaje de riesgo (ej., cuantos indicadores cambiaron su señal)
                change_count = 0
                if sma_rec != prev_sma_rec: change_count += 1
                if rsi_rec != prev_rsi_rec: change_count += 1
                if bb_rec != prev_bb_rec: change_count += 1
                metric_value = (change_count / 3) * 100 if change_count > 0 else 0
                details = f"Recomendación cambió de '{prev_recommendation}' a '{recommendation}'. Indicadores cambiantes: {change_count}/3."

        # 2. Guardar la entrada en el historial general (data.csv)
        with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow([
                timestamp_iso,
                symbol,
                recommendation,
                prev_recommendation, # La recomendación previa guardada
                metric_type,
                round(metric_value, 2), # Redondea el porcentaje
                details
            ])
        
        # 3. Actualizar la última recomendación conocida para este símbolo (last_recommendations.csv)
        # Leer todas las líneas, modificar la del símbolo actual, y reescribir.
        rows = []
        found = False
        if os.path.exists(LAST_REC_FILE):
            with open(LAST_REC_FILE, mode='r', newline='', encoding='utf-8') as file:
                reader = csv.DictReader(file)
                for row in reader:
                    if row['symbol'] == symbol:
                        # Actualiza la fila para el símbolo actual
                        rows.append({
                            'symbol': symbol,
                            'timestamp': timestamp_iso,
                            'recommendation': recommendation,
                            'sma_rec': sma_rec,
                            'rsi_rec': rsi_rec,
                            'bb_rec': bb_rec
                        })
                        found = True
                    else:
                        rows.append(row)
        if not found:
            # Si el símbolo no existía, añade una nueva fila
            rows.append({
                'symbol': symbol,
                'timestamp': timestamp_iso,
                'recommendation': recommendation,
                'sma_rec': sma_rec,
                'rsi_rec': rsi_rec,
                'bb_rec': bb_rec
            })
        
        with open(LAST_REC_FILE, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=['symbol', 'timestamp', 'recommendation', 'sma_rec', 'rsi_rec', 'bb_rec'])
            writer.writeheader() # Escribe los encabezados
            writer.writerows(rows) # Escribe todas las filas

        print(f"[{datetime.now().isoformat()}] Saved and updated last rec for {symbol}: {recommendation}")
        return jsonify({'message': 'Recommendation saved and updated successfully'}), 200

    except Exception as e:
        print(f"Error saving recommendation: {e}")
        return jsonify({'message': f'Internal server error: {str(e)}'}), 500

# Endpoint para obtener las recomendaciones (filtradas por tiempo)
@app.route('/get_recommendations', methods=['GET'])
def get_recommendations():
    recommendations = []
    
    current_time_utc = datetime.now(timezone.utc) 
    threshold_time_utc = current_time_utc - timedelta(hours=HISTORY_DURATION_HOURS)

    try:
        with open(CSV_FILE, mode='r', encoding='utf-8') as file:
            reader = csv.reader(file)
            header = next(reader, None) # Saltar el encabezado (timestamp, symbol, recommendation, prev_recommendation, metric_type, metric_value, details)

            for row in reader:
                if len(row) >= 7: # Ahora esperamos al menos 7 columnas
                    try:
                        timestamp_str, symbol, recommendation, prev_recommendation, metric_type, metric_value, details = row[:7] # Asegurarse de tomar las 7
                        entry_timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
                        
                        if entry_timestamp >= threshold_time_utc:
                            recommendations.append({
                                'timestamp': timestamp_str,
                                'symbol': symbol,
                                'recommendation': recommendation,
                                'prev_recommendation': prev_recommendation,
                                'metric_type': metric_type,
                                'metric_value': float(metric_value), # Convertir a float
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
