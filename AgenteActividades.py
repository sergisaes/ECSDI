# -*- coding: utf-8 -*-
"""
*** Agente de Actividades ***

Este agente recibe peticiones de actividades para una ciudad específica y
responde con una lista de actividades disponibles según los criterios de búsqueda.
Utiliza la API de Amadeus para obtener datos reales de actividades turísticas.

@author: Laura
"""

from multiprocessing import Process, Queue
import socket
import argparse
import datetime
import uuid
import logging
import os
from amadeus import Client, ResponseError

from rdflib import Namespace, Graph, Literal, URIRef, BNode
from rdflib.namespace import RDF, RDFS, XSD, FOAF
from flask import Flask, request, Response, render_template

from AgentUtil.FlaskServer import shutdown_server
from AgentUtil.Agent import Agent
from AgentUtil.ACLMessages import build_message, send_message, get_message_properties
from AgentUtil.ACL import ACL
from AgentUtil.DSO import DSO

# Configurar logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

__author__ = 'Laura'

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
    port = 9010
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
am = Namespace("http://www.amadeus.com/ontology#")

# Diccionario de ciudades y sus coordenadas (latitud, longitud)
CIUDADES_COORDS = {
    'Barcelona': {'ll': '41.3851,2.1734', 'country': 'España'},
    'Madrid': {'ll': '40.4168,-3.7038', 'country': 'España'},
    'Valencia': {'ll': '39.4699,-0.3763', 'country': 'España'},
    'Sevilla': {'ll': '37.3891,-5.9845', 'country': 'España'},
    'Paris': {'ll': '48.8566,2.3522', 'country': 'Francia'},
    'Roma': {'ll': '41.9028,12.4964', 'country': 'Italia'},
    'Londres': {'ll': '51.5074,-0.1278', 'country': 'Reino Unido'},
    'Berlin': {'ll': '52.5200,13.4050', 'country': 'Alemania'},
    'Amsterdam': {'ll': '52.3676,4.9041', 'country': 'Países Bajos'},
    'Lisboa': {'ll': '38.7223,-9.1393', 'country': 'Portugal'},
}

# Mapeo de tipos de actividades para Amadeus
TIPOS_ACTIVIDADES = {
    str(onto.Cultural): 'SIGHTSEEING',
    str(onto.Aventura): 'ADVENTURE',
    str(onto.Gastronomica): 'GASTRONOMY',
    str(onto.Naturaleza): 'OUTDOOR'
}

# Contador de mensajes
mss_cnt = 0

# Datos del Agente
AgenteActividades = Agent('AgenteActividades',
                       agn.AgenteActividades,
                       'http://%s:%d/comm' % (hostname, port),
                       'http://%s:%d/Stop' % (hostname, port))

# Directory agent address
DirectoryAgent = Agent('DirectoryAgent',
                       agn.Directory,
                       'http://%s:%d/Register' % (dhostname, dport),
                       'http://%s:%d/Stop' % (dhostname, dport))

# Inicializar cliente de Amadeus
amadeus = Client(
    client_id='w0fh6OZAxt6MlB8BSGUplHoIebgCco90',
    client_secret='bymSMBr9QNrCvTv0'
)

# Global triplestore graph
dsgraph = Graph()
# Cargar la ontología en el grafo
try:
    dsgraph.parse("entrega2.ttl", format="turtle")
    logger.info("Ontología cargada correctamente")
except Exception as e:
    logger.error(f"Error al cargar la ontología: {e}")

# Base de datos de actividades
activities_db = Graph()
activities_db.bind('am', am)
activities_db.bind('onto', onto)
activities_db.bind('xsd', XSD)

cola1 = Queue()

# Flask app
app = Flask(__name__)


@app.route("/comm")
def comunicacion():
    """
    Punto de entrada de comunicación para recibir peticiones de actividades
    """
    global dsgraph
    global mss_cnt

    message = request.args['content']
    gm = Graph()
    gm.parse(data=message, format='xml')
    
    msgdic = get_message_properties(gm)
    logger.debug(f"Recibido mensaje con performativa: {msgdic['performative']}")

    # Verificar si es una petición de actividades
    if msgdic['performative'] == ACL.request:
        # Buscar el contenido de la petición
        content = msgdic['content']
        
        # Buscar petición de tipo PedirActividades
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionActividad)):
            ciudad_uri = None
            ciudad_nombre = None
            precio_max = None
            tipo_actividad = None
            fecha = None
            franja_horaria = None
            
            # Extraer la ciudad de la petición
            for s1, p1, o1 in gm.triples((s, onto.comoRestriccionLocalidad, None)):
                ciudad_uri = o1
                # Obtener el nombre de la ciudad
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    ciudad_nombre = str(o2)
            
            # Extraer fecha si existe
            for s1, p1, o1 in gm.triples((s, onto.fecha, None)):
                fecha = str(o1)
            
            # Extraer franja horaria si existe
            for s1, p1, o1 in gm.triples((s, onto.franjaHoraria, None)):
                franja_horaria = str(o1)
            
            # Extraer restricciones de precio si existen
            for s1, p1, o1 in gm.triples((s, onto.PrecioMax, None)):
                precio_max = float(o1)
            
            # Extraer tipo de actividad si existe
            for s1, p1, o1 in gm.triples((s, RDF.type, None)):
                if o1 != onto.PeticionActividad and str(o1) in TIPOS_ACTIVIDADES:
                    tipo_actividad = o1

            logger.info(f"Buscando actividades para: Ciudad={ciudad_nombre}, Fecha={fecha}, " +
                      f"Franja={franja_horaria}, Tipo={tipo_actividad}, Precio Máximo={precio_max}")
            
            # Procesar la petición
            respuesta = procesar_peticion_actividades(ciudad_uri, ciudad_nombre, fecha, 
                                                   franja_horaria, precio_max, tipo_actividad, 
                                                   content)
            
            return Response(respuesta, mimetype='text/xml')
    
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
    return "Parando Servidor"


def tidyup():
    """
    Acciones previas a parar el agente
    """
    global cola1
    cola1.put(0)
    try:
        with open("activities_db.ttl", 'wb') as f:
            f.write(activities_db.serialize(format='turtle'))
        logger.info("Base de datos de actividades guardada correctamente")
    except Exception as e:
        logger.error(f"Error al guardar la base de datos: {e}")


def buscar_actividades_amadeus(ciudad_nombre, fecha=None, tipo_actividad=None, precio_max=None, franja_horaria=None):
    """
    Busca actividades en Amadeus API
    
    :param ciudad_nombre: Nombre de la ciudad
    :param fecha: Fecha para la actividad (YYYY-MM-DD)
    :param tipo_actividad: Tipo de actividad
    :param precio_max: Precio máximo
    :param franja_horaria: Franja horaria deseada
    :return: Lista de actividades desde Amadeus
    """
    if not amadeus or ciudad_nombre not in CIUDADES_COORDS:
        logger.warning(f"No se puede buscar en Amadeus: cliente={amadeus}, ciudad={ciudad_nombre}")
        return []
    
    activities = []
    
    try:
        # Obtener coordenadas de la ciudad
        coords = CIUDADES_COORDS[ciudad_nombre]['ll'].split(',')
        latitude = float(coords[0])
        longitude = float(coords[1])
        
        # Consultar actividades usando el endpoint correcto de Amadeus
        logger.info(f"Consultando actividades en Amadeus para {ciudad_nombre} en coordenadas {latitude},{longitude}")
        
        # Usar el método correcto con los parámetros correctos
        response = amadeus.shopping.activities.get(
            latitude=latitude,
            longitude=longitude,
            radius=5  # Radio en kilómetros (máximo 20)
        )
        
        logger.info(f"Recibidas {len(response.data)} actividades de Amadeus")
        
        # Definir franjas horarias disponibles (fijas ya que Amadeus no las proporciona)
        horarios_disponibles = ["09:00", "11:00", "13:00", "15:00", "17:00"]
        
        for activity in response.data:
            # Extraer precio si está disponible
            precio = None
            if 'price' in activity and 'amount' in activity['price']:
                precio = float(activity['price']['amount'])
            
            # Si hay límite de precio y el precio supera el máximo, ignorar
            if precio_max is not None and precio is not None and precio > precio_max:
                logger.debug(f"Actividad {activity.get('name')} excede precio máximo: {precio} > {precio_max}")
                continue
            
            # Verificar franja horaria si se especificó
            if franja_horaria and franja_horaria not in horarios_disponibles:
                logger.debug(f"Actividad {activity.get('name')} no disponible en franja horaria {franja_horaria}")
                continue
            
            # Construir objeto de actividad
            act = {
                'uri': URIRef(f"http://www.amadeus.com/activity/{activity['id']}"),
                'id': activity['id'],
                'nombre': activity.get('name', 'Sin nombre'),
                'tipo': tipo_actividad,
                'precio': precio,
                'descripcion': activity.get('shortDescription', ''),
                'booking_link': activity.get('bookingLink', ''),
                'rating': activity.get('rating', ''),
                'imagen': activity.get('pictures', [''])[0] if activity.get('pictures') else None,
                'coordenadas': {
                    'latitud': activity.get('geoCode', {}).get('latitude'),
                    'longitud': activity.get('geoCode', {}).get('longitude')
                },
                'horarios': horarios_disponibles,
                'horario_seleccionado': franja_horaria if franja_horaria else horarios_disponibles[0],
                'fuente': 'amadeus',
                'fecha': fecha
            }
            
            activities.append(act)
        
        logger.info(f"Encontradas {len(activities)} actividades en Amadeus para {ciudad_nombre}")
        return activities
        
    except ResponseError as e:
        logger.error(f"Error en API de Amadeus: {e}")
        return []
    except Exception as e:
        logger.error(f"Error consultando actividades en Amadeus: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []


def procesar_peticion_actividades(ciudad_uri, ciudad_nombre, fecha, franja_horaria, precio_max, tipo_actividad, content_uri):
    """
    Procesa una petición de actividades y construye una respuesta RDF
    
    :param ciudad_uri: URI de la ciudad en la ontología
    :param ciudad_nombre: Nombre de la ciudad
    :param fecha: Fecha para la actividad
    :param franja_horaria: Franja horaria deseada
    :param precio_max: Precio máximo
    :param tipo_actividad: Tipo de actividad
    :param content_uri: URI del contenido para responder
    :return: Mensaje XML con la respuesta
    """
    global mss_cnt
    
    # Buscar actividades en Amadeus
    activities = []
    if ciudad_nombre in CIUDADES_COORDS:
        activities = buscar_actividades_amadeus(ciudad_nombre, fecha, tipo_actividad, precio_max, franja_horaria)
    else:
        logger.warning(f"No se encontraron coordenadas para la ciudad: {ciudad_nombre}")
    
    # Construir grafo de respuesta
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    g.bind('am', am)
    
    respuesta_id = URIRef(f'respuesta_actividades_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaActividad))
    g.add((respuesta_id, onto.respuestaA, content_uri))
    g.add((respuesta_id, onto.tipoRespuesta, Literal("DarActividades")))
    g.add((respuesta_id, RDFS.comment, Literal(f"Respuesta con {len(activities)} actividades")))
    g.add((respuesta_id, onto.importe, Literal(len(activities), datatype=XSD.integer)))
    
    # Añadir detalles de cada actividad al grafo
    for act in activities:
        # Añadir la actividad a la respuesta
        g.add((respuesta_id, onto.formadoPorActividades, act['uri']))
        
        # Añadir tipo y descripción básica
        g.add((act['uri'], RDF.type, onto.Actividad))
        if act['tipo']:
            g.add((act['uri'], RDF.type, act['tipo']))
        g.add((act['uri'], RDFS.label, Literal(act['nombre'])))
        
        # Añadir precio
        if act['precio'] is not None:
            g.add((act['uri'], onto.Precio, Literal(act['precio'], datatype=XSD.float)))
        
        # Añadir descripción si existe
        if act['descripcion']:
            g.add((act['uri'], RDFS.comment, Literal(act['descripcion'])))
        
        # Añadir horario seleccionado
        g.add((act['uri'], onto.franjaHoraria, Literal(act['horario_seleccionado'])))
        
        # Añadir todos los horarios disponibles
        for i, horario in enumerate(act['horarios']):
            horario_uri = URIRef(f"{act['uri']}/horario_{i}")
            g.add((act['uri'], onto.tieneHorario, horario_uri))
            g.add((horario_uri, RDF.type, onto.FranjaHoraria))
            g.add((horario_uri, onto.hora, Literal(horario)))
        
        # Añadir información adicional de Amadeus
        # Enlace de reserva
        if act['booking_link']:
            g.add((act['uri'], am.bookingLink, Literal(act['booking_link'])))
        
        # Rating si existe
        if act['rating']:
            g.add((act['uri'], am.rating, Literal(float(act['rating']), datatype=XSD.float)))
        
        # Imagen si existe
        if act['imagen']:
            imagen_uri = URIRef(f"{act['uri']}/imagen")
            g.add((act['uri'], am.hasImage, imagen_uri))
            g.add((imagen_uri, RDF.type, am.Image))
            g.add((imagen_uri, am.url, Literal(act['imagen'])))
        
        # Coordenadas
        if 'coordenadas' in act and act['coordenadas'].get('latitud') and act['coordenadas'].get('longitud'):
            geo_uri = URIRef(f"{act['uri']}/geo")
            g.add((act['uri'], am.localizacion, geo_uri))
            g.add((geo_uri, RDF.type, am.GeoLocation))
            g.add((geo_uri, am.latitude, Literal(act['coordenadas']['latitud'], datatype=XSD.float)))
            g.add((geo_uri, am.longitude, Literal(act['coordenadas']['longitud'], datatype=XSD.float)))
        
        # Fecha si existe
        if act['fecha']:
            g.add((act['uri'], onto.fecha, Literal(act['fecha'], datatype=XSD.date)))
        
        # Ciudad en la que se realiza
        g.add((act['uri'], onto.sehaceEn, ciudad_uri))
        
        # Fuente
        g.add((act['uri'], RDFS.isDefinedBy, Literal(act['fuente'])))
        
        # Guardar en la base de datos transiente
        for s, p, o in g.triples((act['uri'], None, None)):
            activities_db.add((s, p, o))
    
    # Construir mensaje completo
    mss_cnt += 1
    logger.info(f"Enviando respuesta con {len(activities)} actividades")
    return build_message(g, ACL.inform,
                         sender=AgenteActividades.uri,
                         receiver=content_uri,
                         content=respuesta_id,
                         msgcnt=mss_cnt).serialize(format='xml')


@app.route("/test", methods=['GET', 'POST'])
def test_interface():
    """
    Interfaz web para probar el agente de actividades
    """
    if request.method == 'GET':
        return '''
        <html>
            <head>
                <title>Test Agente Actividades con Amadeus</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; }
                    .form-group { margin-bottom: 15px; }
                    label { display: block; margin-bottom: 5px; }
                    input, select { padding: 8px; width: 300px; }
                    button { padding: 10px 15px; background-color: #4CAF50; color: white; border: none; cursor: pointer; }
                    h2 { margin-top: 30px; }
                </style>
            </head>
            <body>
                <h1>Test Agente Actividades con API Amadeus</h1>
                
                <form method="post">
                    <div class="form-group">
                        <label>Ciudad:</label>
                        <select name="ciudad">
                            <option value="Barcelona">Barcelona</option>
                            <option value="Madrid">Madrid</option>
                            <option value="Valencia">Valencia</option>
                            <option value="Sevilla">Sevilla</option>
                            <option value="Paris">Paris</option>
                            <option value="Roma">Roma</option>
                            <option value="Londres">Londres</option>
                            <option value="Berlin">Berlin</option>
                            <option value="Amsterdam">Amsterdam</option>
                            <option value="Lisboa">Lisboa</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label>Fecha:</label>
                        <input type="date" name="fecha" required>
                    </div>
                    
                    <div class="form-group">
                        <label>Franja horaria:</label>
                        <select name="franja_horaria">
                            <option value="">Cualquier horario</option>
                            <option value="09:00">09:00</option>
                            <option value="11:00">11:00</option>
                            <option value="13:00">13:00</option>
                            <option value="15:00">15:00</option>
                            <option value="17:00">17:00</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label>Precio máximo (€):</label>
                        <input type="number" name="precio_max" min="1" step="1">
                    </div>
                    
                    <div class="form-group">
                        <label>Tipo de actividad:</label>
                        <select name="tipo_actividad">
                            <option value="">Cualquier tipo</option>
                            <option value="Cultural">Cultural</option>
                            <option value="Aventura">Aventura</option>
                            <option value="Gastronomica">Gastronómica</option>
                            <option value="Naturaleza">Naturaleza</option>
                        </select>
                    </div>
                    
                    <button type="submit">Buscar actividades</button>
                </form>
            </body>
        </html>
        '''
    else:
        # Procesar la petición POST
        ciudad = request.form['ciudad']
        fecha = request.form['fecha']
        franja_horaria = request.form.get('franja_horaria', '')
        precio_max = request.form.get('precio_max', '')
        tipo_actividad_str = request.form.get('tipo_actividad', '')
        
        if precio_max:
            precio_max = float(precio_max)
        else:
            precio_max = None
        
        tipo_actividad = None
        if tipo_actividad_str:
            tipo_actividad = URIRef(onto + tipo_actividad_str)
        
        # Buscar la URI de la ciudad en la ontología
        ciudad_uri = URIRef(onto + ciudad)
        
        # Buscar actividades directamente usando nuestra función
        activities = buscar_actividades_amadeus(ciudad, fecha, tipo_actividad, precio_max, franja_horaria)
        
        # Construir respuesta HTML
        html = f'''
        <html>
            <head>
                <title>Resultados de búsqueda de actividades</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    h1, h2 {{ color: #333; }}
                    .activity-box {{ background: #f9f9f9; padding: 15px; margin: 10px 0; border-radius: 5px; }}
                    .precio {{ font-weight: bold; color: #4CAF50; }}
                    .horarios {{ margin-top: 10px; }}
                    .horario {{ display: inline-block; margin-right: 10px; background: #eee; padding: 3px 8px; border-radius: 3px; }}
                    .source {{ color: #888; font-style: italic; }}
                    .back-btn {{ margin-top: 20px; padding: 10px; background: #4CAF50; color: white; text-decoration: none; display: inline-block; border-radius: 5px; }}
                    .image {{ max-width: 300px; margin-top: 10px; }}
                    .description {{ margin-top: 10px; color: #555; }}
                    .rating {{ color: #F9A825; }}
                </style>
            </head>
            <body>
                <h1>Actividades encontradas</h1>
                <h2>En {ciudad} para {fecha}{' a las ' + franja_horaria if franja_horaria else ''}</h2>
                <p>Total: {len(activities)} actividades</p>
        '''
        
        if not activities:
            html += '''
                <p>No se encontraron actividades que coincidan con los criterios de búsqueda.</p>
            '''
        else:
            for act in activities:
                html += f'''
                    <div class="activity-box">
                        <h3>{act['nombre']}</h3>
                '''
                
                if act['precio'] is not None:
                    html += f'''
                        <p class="precio">Precio: {act['precio']:.2f}€</p>
                    '''
                else:
                    html += f'''
                        <p class="precio">Precio: No especificado</p>
                    '''
                
                if act['rating']:
                    html += f'''
                        <p class="rating">Valoración: {act['rating']}/5.0</p>
                    '''
                
                html += '''
                    <div class="horarios">
                        <p>Horarios disponibles:</p>
                '''
                
                for horario in act['horarios']:
                    html += f'''
                        <span class="horario">{horario}</span>
                    '''
                
                html += '''
                    </div>
                '''
                
                if act['descripcion']:
                    html += f'''
                        <p class="description">{act['descripcion']}</p>
                    '''
                
                if act['imagen']:
                    html += f'''
                        <img src="{act['imagen']}" alt="{act['nombre']}" class="image">
                    '''
                
                if act['booking_link']:
                    html += f'''
                        <p><a href="{act['booking_link']}" target="_blank">Reservar</a></p>
                    '''
                
                html += f'''
                        <p class="source">Fuente: API Amadeus</p>
                    </div>
                '''
        
        html += '''
                <a href="/test" class="back-btn">Nueva búsqueda</a>
            </body>
        </html>
        '''
        
        return html


def agentbehavior1(cola):
    """
    Comportamiento del agente - Registrarse en el directorio
    """
    global mss_cnt
    # Registrar el agente en el servicio de directorio
    gmess = Graph()
    gmess.bind('foaf', FOAF)
    gmess.bind('dso', DSO)
    reg_obj = agn[AgenteActividades.name + '-Register']
    gmess.add((reg_obj, RDF.type, DSO.Register))
    gmess.add((reg_obj, DSO.Uri, AgenteActividades.uri))
    gmess.add((reg_obj, FOAF.name, Literal(AgenteActividades.name)))
    gmess.add((reg_obj, DSO.Address, Literal(AgenteActividades.address)))
    gmess.add((reg_obj, DSO.AgentType, DSO.ActivitiesAgent))

    try:
        send_message(
            build_message(gmess, ACL.request,
                        sender=AgenteActividades.uri,
                        receiver=DirectoryAgent.uri,
                        content=reg_obj,
                        msgcnt=mss_cnt),
            DirectoryAgent.address
        )
        mss_cnt += 1
        logger.info("Agente registrado en el directorio")
    except Exception as e:
        logger.warning(f"No se pudo conectar con el DirectoryAgent: {e}")
        logger.warning("El agente continuará funcionando sin registro en el directorio")
    
    # Bucle principal del comportamiento
    while True:
        try:
            msg = cola.get()
            if msg == 0:
                logger.info("Finalizando comportamiento del agente")
                break
        except Exception as e:
            logger.error(f"Error en el comportamiento del agente: {e}")
            break


if __name__ == '__main__':
    try:
        # Verificar la conexión con Amadeus
        try:
            test_response = amadeus.reference_data.locations.get(
                keyword='Madrid',
                subType=['CITY'],
                page={'limit': 1}
            )
            logger.info(f"Conexión con Amadeus verificada: {test_response.data}")
        except Exception as e:
            logger.error(f"Error al conectar con Amadeus: {e}")
        
        # Poner en marcha los behaviors
        ab1 = Process(target=agentbehavior1, args=(cola1,))
        ab1.start()

        logger.info(f"Iniciando servidor en {hostname}:{port}")
        # Ponemos en marcha el servidor
        app.run(host=hostname, port=port, debug=False)

        # Esperamos a que acaben los behaviors
        ab1.join()
        logger.info('Agente de Actividades finalizado')
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals():
            ab1.terminate()
        print('Error en el Agente de Actividades')