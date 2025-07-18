from flask import Flask, request, jsonify # Importa Flask para la aplicación web, request para manejar peticiones, jsonify para respuestas JSON
from flask_cors import CORS # Importa CORS para manejar las políticas de seguridad entre dominios
import csv # Módulo para trabajar con archivos CSV
import os # Módulo para interactuar con el sistema operativo (ej. verificar si un archivo existe)
from datetime import datetime, timedelta # Módulos para trabajar con fechas y tiempos

# 1. Inicialización de la Aplicación Flask
# La instancia de tu aplicación Flask DEBE llamarse 'app'.
# Gunicorn (el servidor que usa Render) busca específicamente un objeto llamado 'app' en este archivo.
app = Flask(app)

# 2. Configuración de CORS (Cross-Origin Resource Sharing)
# Esto es CRÍTICO para que tu frontend (en GitHub Pages) pueda hacer solicitudes a tu backend (en Render).
# Sin CORS, el navegador bloquearía las solicitudes por razones de seguridad.
# CORS(app) permite que cualquier origen (*) pueda hacer solicitudes a tu API.
CORS(app)

# 3. Configuración del Archivo de "Base de Datos" (CSV)
CSV_FILE = 'data.csv' # Nombre del archivo CSV que actuará como tu base de datos
HISTORY_DURATION_HOURS = 24 # Duración del historial a mantener (24 horas)

# 4. Función para Asegurar que el Archivo CSV Exista
# Esta función verifica si 'data.csv' existe. Si no, lo crea y añade una fila de encabezados.
def ensure_csv_exists():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(['timestamp', 'symbol', 'recommendation']) # Encabezados del CSV

# Llama a la función al inicio para asegurar que el CSV esté listo cuando la app se inicie
ensure_csv_exists()

# 5. Ruta para Guardar una Recomendación (Endpoint POST)
# @app.route define la URL y el método HTTP para esta ruta.
# '/save_recommendation' es la URL a la que tu frontend hará un POST.
@app.route('/save_recommendation', methods=['POST'])
def save_recommendation():
    try:
        # request.get_json() intenta parsear el cuerpo de la petición como JSON.
        data = request.get_json()
        if not data:
            # Si no se envía JSON, devuelve un error 400 (Bad Request).
            return jsonify({'message': 'No data provided'}), 400

        # Extrae los datos del JSON recibido.
        symbol = data.get('symbol')
        recommendation = data.get('recommendation')
        timestamp_str = data.get('timestamp') # El frontend envía el timestamp en formato ISO

        # Verifica que todos los datos necesarios estén presentes.
        if not all([symbol, recommendation, timestamp_str]):
            return jsonify({'message': 'Missing data (symbol, recommendation, timestamp)'}), 400
        
        # Abre el archivo CSV en modo 'a' (append) para añadir nuevas filas.
        # newline='' es importante para evitar filas en blanco adicionales.
        # encoding='utf-8' para manejar caracteres especiales.
        with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow([timestamp_str, symbol, recommendation]) # Escribe la nueva fila

        # Imprime un mensaje en los logs del servidor (Render los mostrará).
        print(f"[{datetime.now().isoformat()}] Saved: {symbol} - {recommendation}")
        # Devuelve una respuesta JSON al frontend con un mensaje de éxito y código 200 (OK).
        return jsonify({'message': 'Recommendation saved successfully'}), 200

    except Exception as e:
        # Captura cualquier error que ocurra durante el proceso y lo imprime.
        print(f"Error saving recommendation: {e}")
        # Devuelve una respuesta de error al frontend con código 500 (Internal Server Error).
        return jsonify({'message': f'Internal server error: {str(e)}'}), 500

# 6. Ruta para Obtener las Recomendaciones (Endpoint GET)
# '/get_recommendations' es la URL a la que tu frontend hará un GET.
@app.route('/get_recommendations', methods=['GET'])
def get_recommendations():
    recommendations = [] # Lista para almacenar las recomendaciones leídas
    current_time = datetime.now() # Hora actual del servidor
    # Calcula el umbral de tiempo para filtrar solo las últimas 24 horas.
    threshold_time = current_time - timedelta(hours=HISTORY_DURATION_HOURS)

    try:
        # Abre el archivo CSV en modo 'r' (read) para leer su contenido.
        with open(CSV_FILE, mode='r', encoding='utf-8') as file:
            reader = csv.reader(file)
            header = next(reader, None) # Lee y salta la fila de encabezados

            # Itera sobre cada fila del CSV.
            for row in reader:
                if len(row) == 3: # Asegura que la fila tenga 3 columnas (timestamp, symbol, recommendation)
                    try:
                        entry_timestamp_str, symbol, recommendation = row
                        # Convierte el timestamp de string a objeto datetime.
                        # .replace('Z', '+00:00') es para manejar el formato ISO de JS que incluye 'Z' (Zulu time).
                        entry_timestamp = datetime.fromisoformat(entry_timestamp_str.replace('Z', '+00:00')) 
                        
                        # Filtra: solo incluye recomendaciones dentro del período de historial.
                        if entry_timestamp >= threshold_time:
                            recommendations.append({
                                'timestamp': entry_timestamp_str, # Mantén el string original para el frontend
                                'symbol': symbol,
                                'recommendation': recommendation
                            })
                    except ValueError as ve:
                        # Si una fila tiene un timestamp mal formado, la salta y registra el error.
                        print(f"Skipping malformed row (timestamp parsing error): {row} - {ve}")
                    except IndexError as ie:
                        # Si una fila no tiene suficientes columnas, la salta y registra el error.
                        print(f"Skipping malformed row (index error): {row} - {ie}")
                else:
                    # Si una fila tiene un número incorrecto de columnas, la salta.
                    print(f"Skipping malformed row (wrong length): {row}")

        # Ordena las recomendaciones por timestamp, de la más nueva a la más antigua.
        recommendations.sort(key=lambda x: datetime.fromisoformat(x['timestamp'].replace('Z', '+00:00')), reverse=True)
        
        # Devuelve la lista de recomendaciones como JSON al frontend.
        return jsonify(recommendations), 200

    except FileNotFoundError:
        # Si el archivo CSV aún no existe (ej. primer inicio), devuelve una lista vacía.
        return jsonify([]), 200
    except Exception as e:
        # Captura cualquier otro error y lo imprime.
        print(f"Error getting recommendations: {e}")
        # Devuelve una respuesta de error al frontend.
        return jsonify({'message': f'Internal server error: {str(e)}'}), 500

# 7. Bloque de Ejecución Principal
# Esto es lo que se ejecuta cuando corres 'python app.py' localmente.
# Render no usa este bloque directamente para iniciar la app (usa Gunicorn),
# pero es útil para pruebas locales.
if __name__ == '__main__':
    # app.run(debug=True) habilita el modo de depuración (útil para desarrollo).
    # host='0.0.0.0' hace que el servidor sea accesible desde cualquier IP (necesario en Render).
    # port=5000 es el puerto por defecto para Flask.
    app.run(debug=True, host='0.0.0.0', port=5000)
