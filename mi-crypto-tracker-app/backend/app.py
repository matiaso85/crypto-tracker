from flask import Flask, request, jsonify
from flask_cors import CORS
import csv
import os
from datetime import datetime, timedelta, timezone # Importar timezone
import pytz # NUEVO: Para manejar zonas horarias, necesitas instalarlo en requirements.txt

app = Flask(__name__)
CORS(app)

CSV_FILE = 'data.csv'
HISTORY_DURATION_HOURS = 24

def ensure_csv_exists():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(['timestamp', 'symbol', 'recommendation'])

ensure_csv_exists()

@app.route('/save_recommendation', methods=['POST'])
def save_recommendation():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'message': 'No data provided'}), 400

        symbol = data.get('symbol')
        recommendation = data.get('recommendation')
        timestamp_str = data.get('timestamp') 

        if not all([symbol, recommendation, timestamp_str]):
            return jsonify({'message': 'Missing data (symbol, recommendation, timestamp)'}), 400
        
        with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow([timestamp_str, symbol, recommendation])
        
        print(f"[{datetime.now().isoformat()}] Saved: {symbol} - {recommendation}")
        return jsonify({'message': 'Recommendation saved successfully'}), 200

    except Exception as e:
        print(f"Error saving recommendation: {e}")
        return jsonify({'message': f'Internal server error: {str(e)}'}), 500

@app.route('/get_recommendations', methods=['GET'])
def get_recommendations():
    recommendations = []
    
    # --- CAMBIO CLAVE AQUÍ ---
    # current_time debe ser consciente de la zona horaria (UTC) para compararlo.
    # Usamos datetime.now(timezone.utc) para obtener la hora actual en UTC y consciente de la zona horaria.
    current_time_utc = datetime.now(timezone.utc) 
    threshold_time_utc = current_time_utc - timedelta(hours=HISTORY_DURATION_HOURS)
    # --- FIN CAMBIO CLAVE ---

    try:
        with open(CSV_FILE, mode='r', encoding='utf-8') as file:
            reader = csv.reader(file)
            header = next(reader, None)

            for row in reader:
                if len(row) == 3:
                    try:
                        entry_timestamp_str, symbol, recommendation = row
                        # Asegurar que entry_timestamp también sea consciente de la zona horaria (UTC).
                        # fromisoformat puede crear objetos conscientes si la cadena tiene info de zona horaria (como 'Z').
                        # Si quieres ser explícito, puedes usar .replace('Z', '+00:00') o convertirlo a UTC.
                        entry_timestamp = datetime.fromisoformat(entry_timestamp_str.replace('Z', '+00:00'))
                        # Asegurarse de que sea consciente del UTC si fromisoformat no lo hace por defecto en algún entorno:
                        if entry_timestamp.tzinfo is None: # Si es naive, lo hacemos aware en UTC
                            entry_timestamp = entry_timestamp.replace(tzinfo=timezone.utc)

                        # Ahora ambas fechas son conscientes de la zona horaria (UTC) y se pueden comparar.
                        if entry_timestamp >= threshold_time_utc:
                            recommendations.append({
                                'timestamp': entry_timestamp_str,
                                'symbol': symbol,
                                'recommendation': recommendation
                            })
                    except ValueError as ve:
                        print(f"Skipping malformed row (timestamp parsing error): {row} - {ve}")
                    except IndexError as ie:
                        print(f"Skipping malformed row (index error): {row} - {ie}")
                else:
                    print(f"Skipping malformed row (wrong length): {row}")

        # La ordenación también debe usar fechas conscientes de la zona horaria.
        # Es mejor usar el objeto datetime directamente para la clave de ordenación.
        recommendations.sort(key=lambda x: datetime.fromisoformat(x['timestamp'].replace('Z', '+00:00')).replace(tzinfo=timezone.utc), reverse=True)
        
        return jsonify(recommendations), 200

    except FileNotFoundError:
        return jsonify([]), 200
    except Exception as e:
        print(f"Error getting recommendations: {e}")
        return jsonify({'message': f'Internal server error: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
