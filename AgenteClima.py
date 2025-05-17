# -*- coding: utf-8 -*-
"""
*** Agente del Clima ***

Este agente recibe peticiones de información meteorológica para una ciudad específica y
responde con datos del tiempo actual y previsiones usando la API de OpenWeatherMap.

@author: Sergi
"""

from multiprocessing import Process, Queue
import socket
import argparse
import datetime
import uuid
import requests
import logging

from rdflib import Namespace, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD, FOAF  # Añadir FOAF aquí
from flask import Flask, request, Response

from AgentUtil.FlaskServer import shutdown_server
from AgentUtil.Agent import Agent
from AgentUtil.ACLMessages import build_message, send_message, get_message_properties
from AgentUtil.ACL import ACL
from AgentUtil.DSO import DSO
from APIKeys import OPENWEATHER_API_KEY

# Configurar logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

__author__ = 'Sergi'

# Configuration stuff
parser = argparse.ArgumentParser()
parser.add_argument('--open', help="Define si el servidor está abierto al exterior o no", action='store_true',
                    default=False)
parser.add_argument('--port', type=int, help="Puerto de comunicación del agente")
parser.add_argument('--dhost', help="Host del agente de directorio")
parser.add_argument('--dport', type=int, help="Puerto del agente de directorio")

args = parser.parse_args()

# Configuración del host y puerto
if args.port is None:
    port = 9002  # Puerto distinto al AgenteActividades
else:
    port = args.port

if args.open:
    hostname = '0.0.0.0'
else:
    hostname = socket.gethostname()

if args.dhost is None:
    dhostname = socket.gethostname()
else:
    dhostname = args.dhost

if args.dport is None:
    dport = 9000
else:
    dport = args.dport

# Definición de los espacios de nombres
agn = Namespace("http://www.agentes.org#")
onto = Namespace("http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2/")

# Configuración de la API de OpenWeatherMap
WEATHER_CURRENT_ENDPOINT = 'http://api.openweathermap.org/data/2.5/weather'
WEATHER_FORECAST_ENDPOINT = 'http://api.openweathermap.org/data/2.5/forecast'

# Contador de mensajes
mss_cnt = 0

# Datos del Agente
AgenteClima = Agent('AgenteClima',
                   agn.AgenteClima,
                   'http://%s:%d/comm' % (hostname, port),
                   'http://%s:%d/Stop' % (hostname, port))

# Directory agent address
DirectoryAgent = Agent('DirectoryAgent',
                       agn.Directory,
                       'http://%s:%d/Register' % (dhostname, dport),
                       'http://%s:%d/Stop' % (dhostname, dport))

# Global triplestore graph
dsgraph = Graph()
# Cargar la ontología en el grafo
try:
    dsgraph.parse("entrega2.ttl", format="turtle")
    logger.info("Ontología cargada correctamente")
except Exception as e:
    logger.error(f"Error al cargar la ontología: {e}")

cola1 = Queue()

# Flask stuff
app = Flask(__name__)


@app.route("/comm")
def comunicacion():
    """
    Punto de entrada de comunicación para recibir peticiones del tiempo
    """
    global dsgraph
    global mss_cnt

    message = request.args['content']
    gm = Graph()
    gm.parse(data=message, format='xml')
    
    msgdic = get_message_properties(gm)
    logger.debug(f"Recibido mensaje con performativa: {msgdic['performative']}")

    # Verificar si es una petición de información meteorológica
    if msgdic['performative'] == ACL.request:
        # Buscar el contenido de la petición
        content = msgdic['content']
        # Buscar en el contenido una petición del clima
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionClima)):
            ciudad_nombre = None
            pais_codigo = 'es'  # Por defecto España
            dias_prevision = 3  # Por defecto 3 días
            
            # Extraer la ciudad de la petición
            for s1, p1, o1 in gm.triples((s, onto.comoRestriccionLocalidad, None)):
                # Obtener el nombre de la ciudad
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    ciudad_nombre = str(o2)
                
                # Obtener el país si está disponible
                for s2, p2, o2 in gm.triples((o1, onto.NombrePais, None)):
                    pais = str(o2)
                    # Convertir nombre del país a código ISO (simplificado)
                    paises_iso = {
                        'España': 'es',
                        'Francia': 'fr',
                        'Italia': 'it',
                        'Reino Unido': 'gb',
                        'Alemania': 'de',
                        'Portugal': 'pt',
                        'Países Bajos': 'nl'
                    }
                    pais_codigo = paises_iso.get(pais, 'es')
            
            # Extraer días de previsión si existen
            for s1, p1, o1 in gm.triples((s, onto.duranteUnTiempo, None)):
                dias_prevision = int(o1) if int(o1) <= 5 else 5  # Máximo 5 días
            
            if ciudad_nombre:
                logger.info(f"Buscando información del clima para: Ciudad={ciudad_nombre}, País={pais_codigo}, Días={dias_prevision}")
                
                # Obtener datos meteorológicos
                datos_clima = obtener_datos_clima(ciudad_nombre, pais_codigo, dias_prevision)
                
                # Construir respuesta
                respuesta = construir_respuesta_clima(datos_clima, content, ciudad_nombre)
                
                # Enviar respuesta
                logger.info(f"Enviando respuesta con datos meteorológicos para {ciudad_nombre}")
                return Response(respuesta, mimetype='text/xml')
            else:
                logger.warning("Petición sin nombre de ciudad")
                return Response(status=400)
    
    # Si no es una petición reconocida, devolver error
    logger.warning("Petición no reconocida")
    return Response(status=400)


@app.route("/Stop")
def stop():
    """
    Entrypoint que para el agente
    """
    tidyup()
    shutdown_server()
    return "Parando Agente del Clima"


def tidyup():
    """
    Acciones previas a parar el agente
    """
    global cola1
    cola1.put(0)


def obtener_datos_clima(ciudad, pais='es', dias=3):
    """
    Obtiene datos meteorológicos usando la API de OpenWeatherMap
    
    :param ciudad: Nombre de la ciudad
    :param pais: Código ISO del país (ej: 'es' para España)
    :param dias: Número de días para la previsión (máximo 5)
    :return: Diccionario con datos meteorológicos
    """
    resultado = {
        'actual': None,
        'prevision': []
    }
    
    try:
        # 1. Obtener el tiempo actual
        params_actual = {
            'q': f"{ciudad},{pais}",
            'units': 'metric',
            'lang': 'es',
            'appid': OPENWEATHER_API_KEY
        }
        
        respuesta_actual = requests.get(WEATHER_CURRENT_ENDPOINT, params=params_actual)
        respuesta_actual.raise_for_status()  # Lanzar excepción si hay error HTTP
        datos_actual = respuesta_actual.json()
        
        # Procesar datos actuales
        resultado['actual'] = {
            'temperatura': datos_actual.get('main', {}).get('temp'),
            'humedad': datos_actual.get('main', {}).get('humidity'),
            'presion': datos_actual.get('main', {}).get('pressure'),
            'descripcion': datos_actual.get('weather', [{}])[0].get('description'),
            'viento': datos_actual.get('wind', {}).get('speed'),
            'nubes': datos_actual.get('clouds', {}).get('all'),
            'icono': datos_actual.get('weather', [{}])[0].get('icon'),
            'fecha': datetime.datetime.now().isoformat()
        }
        
        # 2. Obtener la previsión
        params_prevision = {
            'q': f"{ciudad},{pais}",
            'units': 'metric',
            'lang': 'es',
            'cnt': str(dias * 8),  # 8 mediciones por día (cada 3 horas)
            'appid': OPENWEATHER_API_KEY
        }
        
        respuesta_prevision = requests.get(WEATHER_FORECAST_ENDPOINT, params=params_prevision)
        respuesta_prevision.raise_for_status()
        datos_prevision = respuesta_prevision.json()
        
        # Procesar datos de previsión
        for item in datos_prevision.get('list', []):
            prevision = {
                'temperatura': item.get('main', {}).get('temp'),
                'humedad': item.get('main', {}).get('humidity'),
                'presion': item.get('main', {}).get('pressure'),
                'descripcion': item.get('weather', [{}])[0].get('description'),
                'viento': item.get('wind', {}).get('speed'),
                'nubes': item.get('clouds', {}).get('all'),
                'icono': item.get('weather', [{}])[0].get('icon'),
                'fecha': item.get('dt_txt')
            }
            resultado['prevision'].append(prevision)
        
        # Agrupar previsiones por día (simplificado)
        dias_previstos = {}
        for prev in resultado['prevision']:
            fecha = prev['fecha'].split(' ')[0]  # Obtener solo la fecha, no la hora
            if fecha not in dias_previstos:
                dias_previstos[fecha] = []
            dias_previstos[fecha].append(prev)
        
        # Calcular promedios diarios
        resultado['prevision_diaria'] = []
        for fecha, previsiones in dias_previstos.items():
            # Calcular valores promedio para el día
            temp_avg = sum(p['temperatura'] for p in previsiones) / len(previsiones)
            hum_avg = sum(p['humedad'] for p in previsiones) / len(previsiones)
            # Usar la descripción de la previsión de mediodía (o la primera disponible)
            mediodia = next((p for p in previsiones if '12:00' in p['fecha']), previsiones[0])
            
            dia = {
                'fecha': fecha,
                'temperatura_media': round(temp_avg, 1),
                'humedad_media': round(hum_avg, 1),
                'descripcion': mediodia['descripcion'],
                'icono': mediodia['icono'],
                'temporal_perjudicial': temp_avg < 10 or any('lluvia' in p['descripcion'].lower() or 
                                                          'tormenta' in p['descripcion'].lower() for p in previsiones)
            }
            resultado['prevision_diaria'].append(dia)
        
        return resultado
        
    except Exception as e:
        logger.error(f"Error al obtener datos meteorológicos: {e}")
        return resultado


def construir_respuesta_clima(datos_clima, content_uri, ciudad):
    """
    Construye un mensaje de respuesta con los datos meteorológicos
    
    :param datos_clima: Diccionario con datos obtenidos de OpenWeatherMap
    :param content_uri: URI del contenido de la petición original
    :param ciudad: Nombre de la ciudad
    :return: Mensaje XML con la respuesta
    """
    global mss_cnt
    global AgenteClima
    
    # Crear nuevo grafo para la respuesta
    g = Graph()
    
    # Definir los espacios de nombres
    g.bind('onto', onto)
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('xsd', XSD)
    
    # Crear la respuesta
    respuesta_id = URIRef(f'respuesta_clima_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaClima))
    g.add((respuesta_id, RDFS.comment, Literal(f"Datos meteorológicos para {ciudad}")))
    
    # Añadir datos del tiempo actual si existen
    if datos_clima['actual']:
        clima_actual_id = URIRef(f'clima_actual_{str(uuid.uuid4())}')
        g.add((clima_actual_id, RDF.type, onto.InformacionClima))
        g.add((respuesta_id, onto.climaActual, clima_actual_id))
        
        # Añadir propiedades del tiempo actual
        g.add((clima_actual_id, onto.temperatura, Literal(datos_clima['actual']['temperatura'], datatype=XSD.float)))
        g.add((clima_actual_id, onto.humedad, Literal(datos_clima['actual']['humedad'], datatype=XSD.integer)))
        g.add((clima_actual_id, RDFS.comment, Literal(datos_clima['actual']['descripcion'])))
        g.add((clima_actual_id, onto.velocidadViento, Literal(datos_clima['actual']['viento'], datatype=XSD.float)))
        g.add((clima_actual_id, onto.fecha, Literal(datos_clima['actual']['fecha'], datatype=XSD.dateTime)))
    
    # Añadir datos de previsión diaria si existen
    if 'prevision_diaria' in datos_clima and datos_clima['prevision_diaria']:
        for i, dia in enumerate(datos_clima['prevision_diaria']):
            prevision_id = URIRef(f'prevision_{i}_{str(uuid.uuid4())}')
            g.add((prevision_id, RDF.type, onto.PrediccionClima))
            g.add((respuesta_id, onto.previsiones, prevision_id))
            
            # Añadir propiedades de la previsión
            g.add((prevision_id, onto.fecha, Literal(dia['fecha'], datatype=XSD.date)))
            g.add((prevision_id, onto.temperatura, Literal(dia['temperatura_media'], datatype=XSD.float)))
            g.add((prevision_id, onto.humedad, Literal(dia['humedad_media'], datatype=XSD.integer)))
            g.add((prevision_id, RDFS.comment, Literal(dia['descripcion'])))
            
            # Marcar si hay temporal perjudicial para actividades al aire libre
            g.add((prevision_id, onto.TemporalPerjudicial, Literal(dia['temporal_perjudicial'])))
    
    # Construir el mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform, 
                         sender=AgenteClima.uri, 
                         receiver=content_uri, 
                         msgcnt=mss_cnt).serialize(format='xml')


def agentbehavior1(cola):
    """
    Comportamiento del agente - Registrarse en el directorio
    """
    global mss_cnt
    # Registrar el agente en el servicio de directorio
    gmess = Graph()
    gmess.bind('foaf', FOAF)
    gmess.bind('dso', DSO)
    reg_obj = agn[AgenteClima.name + '-Register']
    gmess.add((reg_obj, RDF.type, DSO.Register))
    gmess.add((reg_obj, DSO.Uri, AgenteClima.uri))
    gmess.add((reg_obj, FOAF.name, Literal(AgenteClima.name)))
    gmess.add((reg_obj, DSO.Address, Literal(AgenteClima.address)))
    gmess.add((reg_obj, DSO.AgentType, DSO.WeatherAgent))

    # Lo metemos en el registro de servicios
    send_message(
        build_message(gmess, ACL.request,
                     sender=AgenteClima.uri,
                     receiver=DirectoryAgent.uri,
                     content=reg_obj,
                     msgcnt=mss_cnt),
        DirectoryAgent.address  # Añadir la dirección del agente directorio
    )
    mss_cnt += 1
    
    logger.info("Agente registrado en el directorio")
    
    # Bucle principal del comportamiento
    while True:
        try:
            # Esperar a un mensaje en la cola
            msg = cola.get()
            if msg == 0:
                logger.info("Finalizando comportamiento del agente")
                break
        except Exception as e:
            logger.error(f"Error en el comportamiento del agente: {e}")
            break


@app.route("/test", methods=['GET', 'POST'])
def test_interface():
    """
    Interfaz web para probar el agente del clima
    """
    if request.method == 'GET':
        # Mostrar un formulario simple para introducir la ciudad y el país
        return '''
        <html>
            <head>
                <title>Test Agente Clima</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; }
                    .form-group { margin-bottom: 15px; }
                    label { display: block; margin-bottom: 5px; }
                    input, select { padding: 8px; width: 300px; }
                    button { padding: 10px 15px; background-color: #4CAF50; color: white; border: none; cursor: pointer; }
                </style>
            </head>
            <body>
                <h1>Test Agente Clima</h1>
                <form method="post">
                    <div class="form-group">
                        <label>Ciudad:</label>
                        <input type="text" name="ciudad" required placeholder="Ej: Barcelona">
                    </div>
                    <div class="form-group">
                        <label>País:</label>
                        <select name="pais">
                            <option value="es">España</option>
                            <option value="fr">Francia</option>
                            <option value="it">Italia</option>
                            <option value="gb">Reino Unido</option>
                            <option value="de">Alemania</option>
                            <option value="pt">Portugal</option>
                            <option value="nl">Países Bajos</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Días de previsión:</label>
                        <input type="number" name="dias" min="1" max="5" value="3">
                    </div>
                    <button type="submit">Consultar clima</button>
                </form>
            </body>
        </html>
        '''
    else:
        # Procesar la petición POST
        ciudad = request.form['ciudad']
        pais = request.form['pais']
        dias = int(request.form['dias'])
        
        # Obtener datos del clima
        datos_clima = obtener_datos_clima(ciudad, pais, dias)
        
        # Construir una respuesta HTML con los resultados
        html = f'''
        <html>
            <head>
                <title>Resultados del Clima para {ciudad}</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    h1, h2 {{ color: #333; }}
                    .clima-box {{ background: #f9f9f9; padding: 15px; margin: 10px 0; border-radius: 5px; }}
                    .prevision {{ display: flex; flex-wrap: wrap; }}
                    .dia-prevision {{ margin: 10px; padding: 15px; background: #f0f0f0; border-radius: 5px; width: 200px; }}
                    .warning {{ color: #ff6600; }}
                    .back-btn {{ margin-top: 20px; padding: 10px; background: #4CAF50; color: white; text-decoration: none; display: inline-block; border-radius: 5px; }}
                </style>
            </head>
            <body>
                <h1>Datos meteorológicos para {ciudad}</h1>
        '''
        
        if datos_clima['actual']:
            html += f'''
                <div class="clima-box">
                    <h2>Clima Actual</h2>
                    <p><strong>Temperatura:</strong> {datos_clima['actual']['temperatura']}°C</p>
                    <p><strong>Humedad:</strong> {datos_clima['actual']['humedad']}%</p>
                    <p><strong>Descripción:</strong> {datos_clima['actual']['descripcion']}</p>
                    <p><strong>Velocidad del viento:</strong> {datos_clima['actual']['viento']} m/s</p>
                </div>
            '''
        
        if 'prevision_diaria' in datos_clima and datos_clima['prevision_diaria']:
            html += '<h2>Previsión para los próximos días</h2><div class="prevision">'
            for dia in datos_clima['prevision_diaria']:
                warning = ""
                if dia['temporal_perjudicial']:
                    warning = '<p class="warning">⚠️ Condiciones adversas para actividades al aire libre</p>'
                
                html += f'''
                <div class="dia-prevision">
                    <h3>{dia['fecha']}</h3>
                    <p><strong>Temperatura media:</strong> {dia['temperatura_media']}°C</p>
                    <p><strong>Humedad media:</strong> {dia['humedad_media']}%</p>
                    <p><strong>Descripción:</strong> {dia['descripcion']}</p>
                    {warning}
                </div>
                '''
            html += '</div>'
        
        html += '''
                <a href="/test" class="back-btn">Volver a consultar</a>
            </body>
        </html>
        '''
        return html


@app.route("/test_peticion")
def test_peticion():
    """
    Crea y envía una petición RDF de prueba al propio agente
    """
    # Crear grafo para la petición
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('onto', onto)
    g.bind('agn', agn)
    
    # Crear la petición
    peticion_id = URIRef('peticion_clima_' + str(uuid.uuid4()))
    g.add((peticion_id, RDF.type, onto.PeticionClima))
    
    # Añadir ciudad (por defecto Barcelona)
    ciudad_param = request.args.get('ciudad', 'Barcelona')
    pais_param = request.args.get('pais', 'España')
    dias_param = request.args.get('dias', '3')
    
    # Crear nodo para la localidad
    ciudad_id = URIRef('ciudad_' + str(uuid.uuid4()))
    g.add((ciudad_id, onto.NombreCiudad, Literal(ciudad_param)))
    g.add((ciudad_id, onto.NombrePais, Literal(pais_param)))
    
    # Vincular con la petición
    g.add((peticion_id, onto.comoRestriccionLocalidad, ciudad_id))
    g.add((peticion_id, onto.duranteUnTiempo, Literal(dias_param)))
    
    # Construir mensaje ACL
    msg = build_message(g, 
                        ACL.request,
                        sender=URIRef('http://test-sender'),
                        receiver=AgenteClima.uri,
                        content=peticion_id,
                        msgcnt=0)
    
    # Mostrar petición y resultado
    xml_msg = msg.serialize(format='xml')
    
    # Hacer la petición al agente
    import requests
    resp = requests.get(AgenteClima.address, params={'content': xml_msg})
    
    html = f'''
    <html>
        <head>
            <title>Prueba de Petición RDF</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                h2 {{ margin-top: 20px; }}
                pre {{ background: #f5f5f5; padding: 10px; overflow-x: auto; }}
                .success {{ color: green; }}
                .error {{ color: red; }}
            </style>
        </head>
        <body>
            <h1>Prueba de Petición RDF al Agente del Clima</h1>
            <p>Ciudad: <strong>{ciudad_param}</strong>, País: <strong>{pais_param}</strong>, Días: <strong>{dias_param}</strong></p>
            
            <h2>Petición RDF enviada:</h2>
            <pre>{xml_msg}</pre>
            
            <h2>Estado de la respuesta: 
                <span class="{'success' if resp.status_code == 200 else 'error'}">
                    {resp.status_code}
                </span>
            </h2>
            
            <h2>Respuesta recibida:</h2>
            <pre>{resp.text if resp.status_code == 200 else "Error en la petición"}</pre>
            
            <p><a href="/test">Volver al formulario de pruebas</a></p>
        </body>
    </html>
    '''
    return html


if __name__ == '__main__':
    try:
        # Poner en marcha los behaviors
        ab1 = Process(target=agentbehavior1, args=(cola1,))
        ab1.start()

        logger.info(f"Iniciando servidor en {hostname}:{port}")
        # Ponemos en marcha el servidor
        app.run(host=hostname, port=port, debug=False)

        # Esperamos a que acaben los behaviors
        ab1.join()
        logger.info('Agente del Clima finalizado')
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals():
            ab1.terminate()
        print('Error en el Agente del Clima')