# -*- coding: utf-8 -*-
"""
*** Agente de Transportes ***

Este agente recibe peticiones para encontrar medios de transporte (principalmente vuelos)
entre origen y destino, y devuelve múltiples opciones.
Utiliza una base de datos RDF de vuelos y aeropuertos.

@author: Sergi
"""

from multiprocessing import Process, Queue
import socket
import argparse
import datetime
import uuid
import requests
import logging
import os
import random
import math
import time
import gzip
from io import BytesIO

from rdflib import Namespace, Graph, Literal, URIRef, BNode
from rdflib.namespace import RDF, RDFS, XSD, FOAF
from flask import Flask, request, Response, render_template

from AgentUtil.FlaskServer import shutdown_server
from AgentUtil.Agent import Agent
from AgentUtil.ACLMessages import build_message, send_message, get_message_properties
from AgentUtil.ACL import ACL
from AgentUtil.DSO import DSO
from AgentUtil.OntoNamespaces import TIO, GEO, DBP

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
parser.add_argument('--datafile', help="Archivo de datos de vuelos", default='../FlightData/FlightRoutes.ttl.gz')

args = parser.parse_args()

# Configuración del host y puerto
if args.port is None:
    port = 9004  # Puerto para AgenteTransportes
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

# Contador de mensajes
mss_cnt = 0

# Datos del Agente
AgenteTransportes = Agent('AgenteTransportes',
                        agn.AgenteTransportes,
                        'http://%s:%d/comm' % (hostname, port),
                        'http://%s:%d/Stop' % (hostname, port))

# Directory agent address
DirectoryAgent = Agent('DirectoryAgent',
                       agn.Directory,
                       'http://%s:%d/Register' % (dhostname, dport),
                       'http://%s:%d/Stop' % (dhostname, dport))

# Base de datos de vuelos como grafo RDF
flight_graph = Graph()
logger.info("Cargando datos de vuelos...")

# Rutas comunes para buscar el archivo (actualizado con los nuevos directorios)
flight_file_paths = [
    args.datafile if args.datafile else None,
    'InfoSources/SPARQL/FlightRoutes.ttl',
    'InfoSources/SPARQL/FlightRoutes.ttl/FlightRoutes.ttl',
    '../InfoSources/SPARQL/FlightRoutes.ttl',
    'Agentes/InfoSources/SPARQL/FlightRoutes.ttl',
    '../Agentes/InfoSources/SPARQL/FlightRoutes.ttl',
    'FlightData/FlightRoutes.ttl.gz',
    '../FlightData/FlightRoutes.ttl.gz'
]

# Intentar cargar el archivo desde diferentes rutas posibles
datos_cargados = False
for file_path in flight_file_paths:
    if file_path is None:
        continue
        
    try:
        if os.path.exists(file_path):
            logger.info(f"Encontrado archivo de vuelos en: {file_path}")
            
            # Determinar formato basado en extensión
            if file_path.endswith('.gz'):
                # Para archivos comprimidos
                with gzip.open(file_path, 'rb') as f:
                    flight_graph.parse(file=f, format='turtle')
            else:
                # Para archivos sin comprimir
                flight_graph.parse(file_path, format='turtle')
                
            logger.info(f"Datos de vuelos cargados correctamente desde {file_path}")
            logger.info(f"El grafo contiene {len(flight_graph)} tripletas")
            datos_cargados = True
            break
    except Exception as e:
        logger.warning(f"No se pudieron cargar los datos desde {file_path}: {e}")

# Si no se han podido cargar datos, usar datos de demostración
if not datos_cargados:
    logger.warning("No se pudieron encontrar datos de vuelos. Usando datos de demostración.")
    demo_data = '''
    @prefix tio: <http://purl.org/tio/ns#> .
    @prefix geo: <http://www.w3.org/2003/01/geo/wgs84_pos#> .
    @prefix dbp: <http://dbpedia.org/ontology/> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
    
    <http://dbpedia.org/resource/Barcelona_El_Prat_Airport> a dbp:Airport ;
        geo:lat "41.3"^^xsd:float ;
        geo:long "2.1"^^xsd:float .
        
    <http://dbpedia.org/resource/Madrid_Barajas_Airport> a dbp:Airport ;
        geo:lat "40.4"^^xsd:float ;
        geo:long "-3.7"^^xsd:float .
        
    <http://dbpedia.org/resource/Paris_Charles_de_Gaulle_Airport> a dbp:Airport ;
        geo:lat "48.8"^^xsd:float ;
        geo:long "2.35"^^xsd:float .
        
    <http://Flights.org/IB1234> a tio:Flight ;
        tio:flightNo "IB1234" ;
        tio:from <http://dbpedia.org/resource/Barcelona_El_Prat_Airport> ;
        tio:to <http://dbpedia.org/resource/Madrid_Barajas_Airport> ;
        tio:operatedBy <http://dbpedia.org/resource/Iberia_Airlines> .
        
    <http://Flights.org/IB1235> a tio:Flight ;
        tio:flightNo "IB1235" ;
        tio:from <http://dbpedia.org/resource/Madrid_Barajas_Airport> ;
        tio:to <http://dbpedia.org/resource/Barcelona_El_Prat_Airport> ;
        tio:operatedBy <http://dbpedia.org/resource/Iberia_Airlines> .
        
    <http://Flights.org/AF1234> a tio:Flight ;
        tio:flightNo "AF1234" ;
        tio:from <http://dbpedia.org/resource/Barcelona_El_Prat_Airport> ;
        tio:to <http://dbpedia.org/resource/Paris_Charles_de_Gaulle_Airport> ;
        tio:operatedBy <http://dbpedia.org/resource/Air_France> .
    '''
    flight_graph.parse(data=demo_data, format='turtle')
    logger.info("Usando datos de demostración")

# Verificación inicial del grafo
if len(flight_graph) > 0:
    # Consulta de prueba para verificar que hay aeropuertos en la base de datos
    test_query = """
    PREFIX geo: <http://www.w3.org/2003/01/geo/wgs84_pos#>
    PREFIX dbp: <http://dbpedia.org/ontology/>
    
    SELECT ?airport ?lat ?lon
    WHERE {
      ?airport a dbp:Airport .
      ?airport geo:lat ?lat .
      ?airport geo:long ?lon .
    }
    LIMIT 5
    """
    
    qres = flight_graph.query(test_query)
    logger.info(f"Encontrados {len(qres)} aeropuertos en la muestra inicial")
    
    for i, row in enumerate(qres):
        airport_uri = str(row['airport'])
        logger.info(f"Aeropuerto {i+1}: {airport_uri}")
else:
    logger.error("El grafo de vuelos está vacío")

# Global triplestore graph para la ontología del dominio
dsgraph = Graph()
# Cargar la ontología en el grafo
try:
    dsgraph.parse("entrega2.ttl", format="turtle")
    logger.info("Ontología cargada correctamente")
except Exception as e:
    logger.error(f"Error al cargar la ontología: {e}")

# Base de datos RDF para almacenar reservas y vuelos seleccionados
transport_db = Graph()
transport_db.bind('tio', TIO)
transport_db.bind('geo', GEO)
transport_db.bind('dbp', DBP)
transport_db.bind('onto', onto)
transport_db.bind('xsd', XSD)

# Cola para comunicación entre procesos
cola1 = Queue()

# Flask app
app = Flask(__name__)

@app.route("/comm")
def comunicacion():
    """
    Punto de entrada de comunicación para recibir peticiones de transporte
    """
    global dsgraph
    global mss_cnt

    message = request.args['content']
    gm = Graph()
    gm.parse(data=message, format='xml')
    
    msgdic = get_message_properties(gm)
    logger.debug(f"Recibido mensaje con performativa: {msgdic['performative']}")

    # Verificar si es una petición de transportes
    if msgdic['performative'] == ACL.request:
        # Buscar el contenido de la petición
        content = msgdic['content']
        
        # Buscar petición de transporte
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionTransporte)):
            # Extraer información de la petición
            origen = None
            destino = None
            fecha_ida = None
            fecha_vuelta = None
            precio_max = None
            
            # Obtener origen
            for s1, p1, o1 in gm.triples((s, onto.comoOrigen, None)):
                # Buscar coordenadas de la ciudad origen
                origen_lat = None
                origen_lon = None
                origen_nombre = None
                
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    origen_nombre = str(o2)
                
                # Si no tenemos coordenadas directas, usar un mapeo simple
                ciudades = {
                    'Barcelona': {'lat': 41.3851, 'lon': 2.1734},
                    'Madrid': {'lat': 40.4168, 'lon': -3.7038},
                    'Valencia': {'lat': 39.4699, 'lon': 0.3763},
                    'Sevilla': {'lat': 37.3891, 'lon': -5.9845},
                    'Paris': {'lat': 48.8566, 'lon': 2.3522},
                    'Roma': {'lat': 41.9028, 'lon': 12.4964},
                    'Londres': {'lat': 51.5074, 'lon': -0.1278},
                    'Berlin': {'lat': 52.5200, 'lon': 13.4050},
                    'Amsterdam': {'lat': 52.3676, 'lon': 4.9041},
                    'Lisboa': {'lat': 38.7223, 'lon': -9.1393},
                }
                
                if origen_nombre in ciudades:
                    origen_lat = ciudades[origen_nombre]['lat']
                    origen_lon = ciudades[origen_nombre]['lon']
                    origen = {'nombre': origen_nombre, 'lat': origen_lat, 'lon': origen_lon}
            
            # Obtener destino
            for s1, p1, o1 in gm.triples((s, onto.comoDestino, None)):
                # Buscar coordenadas de la ciudad destino
                destino_lat = None
                destino_lon = None
                destino_nombre = None
                
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    destino_nombre = str(o2)
                
                # Si no tenemos coordenadas directas, usar un mapeo simple
                if destino_nombre in ciudades:
                    destino_lat = ciudades[destino_nombre]['lat']
                    destino_lon = ciudades[destino_nombre]['lon']
                    destino = {'nombre': destino_nombre, 'lat': destino_lat, 'lon': destino_lon}
            
            # Obtener fechas
            for s1, p1, o1 in gm.triples((s, onto.fecha_inicio, None)):
                fecha_ida = str(o1)
            
            for s1, p1, o1 in gm.triples((s, onto.fecha_fin, None)):
                fecha_vuelta = str(o1)
            
            # Obtener precio máximo si existe
            for s1, p1, o1 in gm.triples((s, onto.PrecioMax, None)):
                precio_max = float(o1)
            
            if origen and destino:
                # Procesar petición y buscar vuelos
                respuesta = procesar_peticion_transporte(origen, destino, fecha_ida, fecha_vuelta, precio_max, content)
                return Response(respuesta, mimetype='text/xml')
            else:
                logger.warning("Petición incompleta: falta origen o destino")
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
    return "Parando Agente de Transportes"


def tidyup():
    """
    Acciones previas a parar el agente
    """
    global cola1
    cola1.put(0)
    # Guardar datos antes de terminar
    try:
        with open("transport_db.ttl", 'wb') as f:
            f.write(transport_db.serialize(format='turtle'))
        logger.info("Base de datos de transportes guardada correctamente")
    except Exception as e:
        logger.error(f"Error al guardar la base de datos: {e}")


def buscar_aeropuerto_cercano(lat, lon, radio_km=100):
    """
    Busca aeropuertos dentro de un radio de coordenadas dadas
    
    :param lat: Latitud
    :param lon: Longitud
    :param radio_km: Radio en kilómetros
    :return: Lista de URIs de aeropuertos
    """
    # Calculamos un aproximado en grados para el radio (conversión de km a grados)
    radio_lat = radio_km / 111.0
    radio_lon = radio_km / (111.0 * math.cos(math.radians(lat)))
    
    lat_min = lat - radio_lat
    lat_max = lat + radio_lat
    lon_min = lon - radio_lon
    lon_max = lon + radio_lon
    
    # Consulta SPARQL para encontrar aeropuertos en el área (siguiendo el formato del ejemplo)
    query = f"""
    PREFIX geo: <http://www.w3.org/2003/01/geo/wgs84_pos#>
    PREFIX dbp: <http://dbpedia.org/ontology/>
    
    SELECT ?f ?lat ?lon
    WHERE {{
      ?f rdf:type dbp:Airport .
      ?f geo:lat ?lat .
      ?f geo:long ?lon .
      FILTER ( ?lat < {lat_max} &&
              ?lat > {lat_min} &&
              ?lon < {lon_max} &&
              ?lon > {lon_min})
    }}
    LIMIT 30
    """
    
    try:
        qres = flight_graph.query(query)
        logger.info(f"Encontrados {len(qres)} aeropuertos cercanos a ({lat}, {lon})")
        
        aeropuertos = []
        for row in qres:
            airport_uri = row['f']
            airport_lat = float(row['lat'])
            airport_lon = float(row['lon'])
            
            # Calcular distancia exacta usando la fórmula haversine
            R = 6371.0  # Radio de la Tierra en km
            dlat = math.radians(airport_lat - lat)
            dlon = math.radians(airport_lon - lon)
            a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(airport_lat)) * math.sin(dlon / 2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            distancia = R * c  # Distancia en km
            
            # Extraer nombre del aeropuerto desde la URI (para mostrar un nombre más legible)
            nombre = str(airport_uri).split('/')[-1].replace('_Airport', '').replace('_', ' ')
            
            # Consulta a DBpedia para obtener información adicional sobre el aeropuerto
            info_adicional = consultar_dbpedia(airport_uri)
            
            aeropuertos.append({
                'uri': airport_uri,
                'lat': airport_lat,
                'lon': airport_lon,
                'distancia': distancia,
                'nombre': nombre,
                'info': info_adicional
            })
        
        # Ordenar por distancia
        aeropuertos.sort(key=lambda x: x['distancia'])
        return aeropuertos
    
    except Exception as e:
        logger.error(f"Error al buscar aeropuertos cercanos: {e}")
        return []


def buscar_vuelos(origen_airport, destino_airport):
    """
    Busca vuelos entre dos aeropuertos usando el formato del ejemplo
    
    :param origen_airport: URI del aeropuerto de origen
    :param destino_airport: URI del aeropuerto de destino
    :return: Lista de vuelos
    """
    # Consulta como en el ejemplo para buscar vuelos
    query = f"""
    PREFIX tio: <http://purl.org/tio/ns#>
    
    SELECT ?f ?flightNo ?o
    WHERE {{
      ?f rdf:type tio:Flight .
      ?f tio:from <{origen_airport}> .
      ?f tio:to <{destino_airport}> .
      ?f tio:flightNo ?flightNo .
      ?f tio:operatedBy ?o .
    }}
    """
    
    try:
        qres = flight_graph.query(query, initNs=dict(tio=TIO))
        vuelos = []
        
        for row in qres:
            # Extraer nombre de la aerolínea desde la URI
            airline_name = str(row['o']).split('/')[-1].replace('_', ' ')
            
            vuelos.append({
                'uri': row['f'],
                'numero': str(row['flightNo']),
                'aerolinea': airline_name,
                'aerolinea_uri': row['o']
            })
        
        logger.info(f"Encontrados {len(vuelos)} vuelos de {origen_airport} a {destino_airport}")
        
        # Si no hay vuelos directos, buscar vuelos con una escala
        if not vuelos:
            vuelos = buscar_vuelos_con_escala(origen_airport, destino_airport)
        
        return vuelos
    
    except Exception as e:
        logger.error(f"Error al buscar vuelos: {e}")
        return []


def buscar_vuelos_con_escala(origen_airport, destino_airport):
    """
    Busca vuelos con una escala entre origen y destino
    """
    query = f"""
    PREFIX tio: <http://purl.org/tio/ns#>
    
    SELECT ?f1 ?f2 ?escala ?flightNo1 ?flightNo2 ?o1 ?o2
    WHERE {{
      ?f1 rdf:type tio:Flight .
      ?f1 tio:from <{origen_airport}> .
      ?f1 tio:to ?escala .
      ?f1 tio:flightNo ?flightNo1 .
      ?f1 tio:operatedBy ?o1 .
      
      ?f2 rdf:type tio:Flight .
      ?f2 tio:from ?escala .
      ?f2 tio:to <{destino_airport}> .
      ?f2 tio:flightNo ?flightNo2 .
      ?f2 tio:operatedBy ?o2 .
      
      FILTER (?escala != <{origen_airport}> && ?escala != <{destino_airport}>)
    }}
    LIMIT 5
    """
    
    try:
        qres = flight_graph.query(query, initNs=dict(tio=TIO))
        vuelos_con_escala = []
        
        for row in qres:
            vuelos_con_escala.append({
                'tipo': 'con_escala',
                'uri1': row['f1'],
                'uri2': row['f2'],
                'escala': row['escala'],
                'numero1': str(row['flightNo1']),
                'numero2': str(row['flightNo2']),
                'aerolinea1': str(row['o1']).split('/')[-1].replace('_', ' '),
                'aerolinea2': str(row['o2']).split('/')[-1].replace('_', ' ')
            })
        
        logger.info(f"Encontrados {len(vuelos_con_escala)} vuelos con escala de {origen_airport} a {destino_airport}")
        return vuelos_con_escala
    
    except Exception as e:
        logger.error(f"Error al buscar vuelos con escala: {e}")
        return []


def generar_detalles_vuelo(vuelo, fecha, tipo_vuelo):
    """
    Genera detalles aleatorios para un vuelo
    
    :param vuelo: Información básica del vuelo
    :param fecha: Fecha del vuelo
    :param tipo_vuelo: 'ida' o 'vuelta'
    :return: Diccionario con detalles del vuelo
    """
    # Generar hora de salida y duración aleatorias
    hora_salida = f"{random.randint(6, 21):02d}:{random.randint(0, 59):02d}"
    duracion_minutos = random.randint(60, 240)  # Entre 1 y 4 horas
    
    # Calcular hora de llegada
    hora_salida_minutos = int(hora_salida.split(':')[0]) * 60 + int(hora_salida.split(':')[1])
    hora_llegada_minutos = (hora_salida_minutos + duracion_minutos) % (24 * 60)
    hora_llegada = f"{hora_llegada_minutos // 60:02d}:{hora_llegada_minutos % 60:02d}"
    
    # Generar precio aleatorio
    precio_base = random.uniform(50, 400)
    
    # Ajustar según tipo de vuelo
    if tipo_vuelo == 'ida':
        precio = round(precio_base, 2)
    else:
        precio = round(precio_base * 0.9, 2)  # La vuelta suele ser más barata
    
    return {
        **vuelo,
        'fecha': fecha,
        'hora_salida': hora_salida,
        'hora_llegada': hora_llegada,
        'duracion_minutos': duracion_minutos,
        'precio': precio,
        'tipo': tipo_vuelo
    }


def procesar_peticion_transporte(origen, destino, fecha_ida, fecha_vuelta, precio_max, content_uri):
    """
    Procesa una petición de transporte buscando vuelos adecuados
    
    :param origen: Información del origen (coordenadas)
    :param destino: Información del destino (coordenadas)
    :param fecha_ida: Fecha de ida
    :param fecha_vuelta: Fecha de vuelta
    :param precio_max: Precio máximo (opcional)
    :param content_uri: URI del contenido para responder
    :return: Mensaje XML con la respuesta
    """
    global mss_cnt
    
    logger.info(f"Buscando transporte desde {origen['nombre']} hacia {destino['nombre']}")
    
    # Buscar aeropuertos cercanos al origen
    aeropuertos_origen = buscar_aeropuerto_cercano(origen['lat'], origen['lon'])
    
    # Buscar aeropuertos cercanos al destino
    aeropuertos_destino = buscar_aeropuerto_cercano(destino['lat'], destino['lon'])
    
    if not aeropuertos_origen or not aeropuertos_destino:
        logger.warning("No se encontraron aeropuertos cercanos al origen o destino")
        # Crear una respuesta de error
        g = Graph()
        g.bind('rdf', RDF)
        g.bind('onto', onto)
        
        respuesta_id = URIRef(f'respuesta_transporte_{str(uuid.uuid4())}')
        g.add((respuesta_id, RDF.type, onto.RespuestaTransporte))
        g.add((respuesta_id, RDFS.comment, Literal("No se encontraron aeropuertos cercanos")))
        
        mss_cnt += 1
        return build_message(g, ACL.inform,
                            sender=AgenteTransportes.uri,
                            receiver=content_uri,
                            msgcnt=mss_cnt).serialize(format='xml')
    
    # Buscar vuelos de ida
    vuelos_ida = []
    for origen_ap in aeropuertos_origen[:3]:  # Limitar a 3 aeropuertos por eficiencia
        for destino_ap in aeropuertos_destino[:3]:
            vuelos = buscar_vuelos(origen_ap['uri'], destino_ap['uri'])
            for v in vuelos:
                v_detallado = generar_detalles_vuelo(v, fecha_ida, 'ida')
                v_detallado['aeropuerto_origen'] = origen_ap
                v_detallado['aeropuerto_destino'] = destino_ap
                vuelos_ida.append(v_detallado)
    
    # Buscar vuelos de vuelta
    vuelos_vuelta = []
    for destino_ap in aeropuertos_destino[:3]:  # Limitar a 3 aeropuertos por eficiencia
        for origen_ap in aeropuertos_origen[:3]:
            vuelos = buscar_vuelos(destino_ap['uri'], origen_ap['uri'])
            for v in vuelos:
                v_detallado = generar_detalles_vuelo(v, fecha_vuelta, 'vuelta')
                v_detallado['aeropuerto_origen'] = destino_ap
                v_detallado['aeropuerto_destino'] = origen_ap
                vuelos_vuelta.append(v_detallado)
    
    # Si no hay vuelos disponibles, crear algunos ficticios para demostración
    if not vuelos_ida:
        aerop_origen = aeropuertos_origen[0] if aeropuertos_origen else {'uri': URIRef('http://example.org/airport/origin'), 'nombre': origen['nombre'], 'distancia': 0}
        aerop_destino = aeropuertos_destino[0] if aeropuertos_destino else {'uri': URIRef('http://example.org/airport/destination'), 'nombre': destino['nombre'], 'distancia': 0}
        vuelos_ida = [{
            'uri': URIRef(f'http://Flights.org/IB{random.randint(1000, 9999)}'),
            'numero': f'IB{random.randint(1000, 9999)}',
            'aerolinea': 'Iberia Airlines',
            'fecha': fecha_ida,
            'hora_salida': f"{random.randint(6, 21):02d}:{random.randint(0, 59):02d}",
            'hora_llegada': f"{random.randint(6, 21):02d}:{random.randint(0, 59):02d}",
            'duracion_minutos': random.randint(60, 240),
            'precio': random.uniform(50, 400),
            'tipo': 'ida',
            'aeropuerto_origen': aerop_origen,
            'aeropuerto_destino': aerop_destino
        }]
    
    if not vuelos_vuelta:
        aerop_destino = aeropuertos_destino[0] if aeropuertos_destino else {'uri': URIRef('http://example.org/airport/destination'), 'nombre': destino['nombre'], 'distancia': 0}
        aerop_origen = aeropuertos_origen[0] if aeropuertos_origen else {'uri': URIRef('http://example.org/airport/origin'), 'nombre': origen['nombre'], 'distancia': 0}
        vuelos_vuelta = [{
            'uri': URIRef(f'http://Flights.org/IB{random.randint(1000, 9999)}'),
            'numero': f'IB{random.randint(1000, 9999)}',
            'aerolinea': 'Iberia Airlines',
            'fecha': fecha_vuelta,
            'hora_salida': f"{random.randint(6, 21):02d}:{random.randint(0, 59):02d}",
            'hora_llegada': f"{random.randint(6, 21):02d}:{random.randint(0, 59):02d}",
            'duracion_minutos': random.randint(60, 240),
            'precio': random.uniform(50, 400),
            'tipo': 'vuelta',
            'aeropuerto_origen': aerop_destino,
            'aeropuerto_destino': aerop_origen
        }]
    
    # Filtrar por precio máximo si se ha especificado
    if precio_max is not None:
        vuelos_ida = [v for v in vuelos_ida if v['precio'] <= precio_max / 2]
        vuelos_vuelta = [v for v in vuelos_vuelta if v['precio'] <= precio_max / 2]
    
    # Limitar el número de vuelos a devolver
    vuelos_ida = vuelos_ida[:5]
    vuelos_vuelta = vuelos_vuelta[:5]
    
    # Generar respuesta en RDF
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    g.bind('tio', TIO)
    
    # Crear la respuesta
    respuesta_id = URIRef(f'respuesta_transporte_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaTransporte))
    
    # Añadir los vuelos de ida
    for vuelo in vuelos_ida:
        vuelo_id = URIRef(f"vuelo_ida_{str(uuid.uuid4())}")
        g.add((vuelo_id, RDF.type, onto.Avion))  # Todos los vuelos son tipo Avion
        g.add((respuesta_id, onto.formadoPorTransportes, vuelo_id))
        
        # Detalles del vuelo
        g.add((vuelo_id, onto.Precio, Literal(vuelo['precio'], datatype=XSD.float)))
        g.add((vuelo_id, RDFS.label, Literal(f"Vuelo {vuelo['numero']} - {vuelo['aerolinea']}")))
        g.add((vuelo_id, onto.Salida, Literal(f"{vuelo['fecha']}T{vuelo['hora_salida']}:00", datatype=XSD.dateTime)))
        g.add((vuelo_id, onto.Llegada, Literal(f"{vuelo['fecha']}T{vuelo['hora_llegada']}:00", datatype=XSD.dateTime)))
        
        # Relacionar con aeropuertos
        airport_origen_uri = vuelo['aeropuerto_origen']['uri']
        airport_destino_uri = vuelo['aeropuerto_destino']['uri']
        
        g.add((vuelo_id, onto.saleDe, URIRef(airport_origen_uri)))
        g.add((vuelo_id, onto.llegaA, URIRef(airport_destino_uri)))
        
        # Más detalles
        g.add((vuelo_id, RDFS.comment, Literal(f"Vuelo operado por {vuelo['aerolinea']}")))
        g.add((vuelo_id, onto.IdVuelo, Literal(vuelo['numero'])))
    
    # Añadir los vuelos de vuelta
    for vuelo in vuelos_vuelta:
        vuelo_id = URIRef(f"vuelo_vuelta_{str(uuid.uuid4())}")
        g.add((vuelo_id, RDF.type, onto.Avion))
        g.add((respuesta_id, onto.formadoPorTransportes, vuelo_id))
        
        # Detalles del vuelo
        g.add((vuelo_id, onto.Precio, Literal(vuelo['precio'], datatype=XSD.float)))
        g.add((vuelo_id, RDFS.label, Literal(f"Vuelo {vuelo['numero']} - {vuelo['aerolinea']}")))
        g.add((vuelo_id, onto.Salida, Literal(f"{vuelo['fecha']}T{vuelo['hora_salida']}:00", datatype=XSD.dateTime)))
        g.add((vuelo_id, onto.Llegada, Literal(f"{vuelo['fecha']}T{vuelo['hora_llegada']}:00", datatype=XSD.dateTime)))
        
        # Relacionar con aeropuertos
        airport_origen_uri = vuelo['aeropuerto_origen']['uri']
        airport_destino_uri = vuelo['aeropuerto_destino']['uri']
        
        g.add((vuelo_id, onto.saleDe, URIRef(airport_origen_uri)))
        g.add((vuelo_id, onto.llegaA, URIRef(airport_destino_uri)))
        
        # Más detalles
        g.add((vuelo_id, RDFS.comment, Literal(f"Vuelo operado por {vuelo['aerolinea']}")))
        g.add((vuelo_id, onto.IdVuelo, Literal(vuelo['numero'])))
    
    # Añadir la respuesta a la base de datos local
    respuesta_uri = URIRef(f"respuesta_{str(uuid.uuid4())}")
    transport_db.add((respuesta_uri, RDF.type, onto.RespuestaTransporte))
    transport_db.add((respuesta_uri, RDFS.label, Literal(f"Transportes de {origen['nombre']} a {destino['nombre']}")))
    transport_db.add((respuesta_uri, RDFS.comment, Literal(f"Búsqueda realizada el {datetime.datetime.now().isoformat()}")))
    
    # Construir mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform,
                        sender=AgenteTransportes.uri,
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
    reg_obj = agn[AgenteTransportes.name + '-Register']
    gmess.add((reg_obj, RDF.type, DSO.Register))
    gmess.add((reg_obj, DSO.Uri, AgenteTransportes.uri))
    gmess.add((reg_obj, FOAF.name, Literal(AgenteTransportes.name)))
    gmess.add((reg_obj, DSO.Address, Literal(AgenteTransportes.address)))
    gmess.add((reg_obj, DSO.AgentType, DSO.TransportAgent))

    # Lo metemos en el registro de servicios
    try:
        send_message(
            build_message(gmess, ACL.request,
                        sender=AgenteTransportes.uri,
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
    Interfaz web para probar el agente de transportes
    """
    if request.method == 'GET':
        # Mostrar un formulario para pruebas
        return '''
        <html>
            <head>
                <title>Test Agente Transportes</title>
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
                <h1>Test Agente Transportes</h1>
                
                <form method="post">
                    <div class="form-group">
                        <label>Ciudad origen:</label>
                        <select name="origen">
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
                        <label>Ciudad destino:</label>
                        <select name="destino">
                            <option value="Madrid">Madrid</option>
                            <option value="Barcelona">Barcelona</option>
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
                        <label>Fecha ida:</label>
                        <input type="date" name="fecha_ida" required>
                    </div>
                    
                    <div class="form-group">
                        <label>Fecha vuelta:</label>
                        <input type="date" name="fecha_vuelta" required>
                    </div>
                    
                    <div class="form-group">
                        <label>Precio máximo (€):</label>
                        <input type="number" name="precio_max" min="1" step="1">
                    </div>
                    
                    <button type="submit">Buscar vuelos</button>
                </form>
            </body>
        </html>
        '''
    else:
        # Procesar la petición POST
        origen_nombre = request.form['origen']
        destino_nombre = request.form['destino']
        fecha_ida = request.form['fecha_ida']
        fecha_vuelta = request.form['fecha_vuelta']
        precio_max = request.form.get('precio_max')
        
        if precio_max:
            precio_max = float(precio_max)
        
        # Mapeo de ciudades a coordenadas
        ciudades = {
            'Barcelona': {'lat': 41.3851, 'lon': 2.1734},
            'Madrid': {'lat': 40.4168, 'lon': -3.7038},
            'Valencia': {'lat': 39.4699, 'lon': 0.3763},
            'Sevilla': {'lat': 37.3891, 'lon': -5.9845},
            'Paris': {'lat': 48.8566, 'lon': 2.3522},
            'Roma': {'lat': 41.9028, 'lon': 12.4964},
            'Londres': {'lat': 51.5074, 'lon': -0.1278},
            'Berlin': {'lat': 52.5200, 'lon': 13.4050},
            'Amsterdam': {'lat': 52.3676, 'lon': 4.9041},
            'Lisboa': {'lat': 38.7223, 'lon': -9.1393},
        }
        
        origen = {'nombre': origen_nombre, 'lat': ciudades[origen_nombre]['lat'], 'lon': ciudades[origen_nombre]['lon']}
        destino = {'nombre': destino_nombre, 'lat': ciudades[destino_nombre]['lat'], 'lon': ciudades[destino_nombre]['lon']}
        
        # Buscar aeropuertos cercanos
        aeropuertos_origen = buscar_aeropuerto_cercano(origen['lat'], origen['lon'])
        aeropuertos_destino = buscar_aeropuerto_cercano(destino['lat'], destino['lon'])
        
        # Buscar vuelos de ida
        vuelos_ida = []
        for origen_ap in aeropuertos_origen[:2]:
            for destino_ap in aeropuertos_destino[:2]:
                vuelos = buscar_vuelos(origen_ap['uri'], destino_ap['uri'])
                for v in vuelos:
                    v_detallado = generar_detalles_vuelo(v, fecha_ida, 'ida')
                    v_detallado['aeropuerto_origen'] = origen_ap
                    v_detallado['aeropuerto_destino'] = destino_ap
                    vuelos_ida.append(v_detallado)
        
        # Buscar vuelos de vuelta
        vuelos_vuelta = []
        for destino_ap in aeropuertos_destino[:2]:
            for origen_ap in aeropuertos_origen[:2]:
                vuelos = buscar_vuelos(destino_ap['uri'], origen_ap['uri'])
                for v in vuelos:
                    v_detallado = generar_detalles_vuelo(v, fecha_vuelta, 'vuelta')
                    v_detallado['aeropuerto_origen'] = destino_ap
                    v_detallado['aeropuerto_destino'] = origen_ap
                    vuelos_vuelta.append(v_detallado)
        
        # Si no hay vuelos disponibles, crear algunos ficticios
        if not vuelos_ida:
            aerop_origen = aeropuertos_origen[0] if aeropuertos_origen else {'uri': 'http://example.org/airport/origin', 'nombre': origen_nombre, 'distancia': 0}
            aerop_destino = aeropuertos_destino[0] if aeropuertos_destino else {'uri': 'http://example.org/airport/destination', 'nombre': destino_nombre, 'distancia': 0}
            
            for i in range(3):
                vuelos_ida.append({
                    'uri': f'http://Flights.org/IB{random.randint(1000, 9999)}',
                    'numero': f'IB{random.randint(1000, 9999)}',
                    'aerolinea': 'Iberia Airlines',
                    'fecha': fecha_ida,
                    'hora_salida': f"{random.randint(6, 21):02d}:{random.randint(0, 59):02d}",
                    'hora_llegada': f"{random.randint(6, 21):02d}:{random.randint(0, 59):02d}",
                    'duracion_minutos': random.randint(60, 240),
                    'precio': random.uniform(50, 400),
                    'tipo': 'ida',
                    'aeropuerto_origen': aerop_origen,
                    'aeropuerto_destino': aerop_destino
                })
        
        if not vuelos_vuelta:
            aerop_destino = aeropuertos_destino[0] if aeropuertos_destino else {'uri': 'http://example.org/airport/destination', 'nombre': destino_nombre, 'distancia': 0}
            aerop_origen = aeropuertos_origen[0] if aeropuertos_origen else {'uri': 'http://example.org/airport/origin', 'nombre': origen_nombre, 'distancia': 0}
            
            for i in range(3):
                vuelos_vuelta.append({
                    'uri': f'http://Flights.org/IB{random.randint(1000, 9999)}',
                    'numero': f'IB{random.randint(1000, 9999)}',
                    'aerolinea': 'Iberia Airlines',
                    'fecha': fecha_vuelta,
                    'hora_salida': f"{random.randint(6, 21):02d}:{random.randint(0, 59):02d}",
                    'hora_llegada': f"{random.randint(6, 21):02d}:{random.randint(0, 59):02d}",
                    'duracion_minutos': random.randint(60, 240),
                    'precio': random.uniform(50, 400),
                    'tipo': 'vuelta',
                    'aeropuerto_origen': aerop_destino,
                    'aeropuerto_destino': aerop_origen
                })
        
        # Filtrar por precio máximo si se ha especificado
        if precio_max is not None:
            vuelos_ida = [v for v in vuelos_ida if v['precio'] <= precio_max / 2]
            vuelos_vuelta = [v for v in vuelos_vuelta if v['precio'] <= precio_max / 2]
        
        # Construir respuesta HTML
        html = f'''
        <html>
            <head>
                <title>Resultados de búsqueda de vuelos</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    h1, h2 {{ color: #333; }}
                    .vuelo-box {{ background: #f9f9f9; padding: 15px; margin: 10px 0; border-radius: 5px; }}
                    .ida-vuelos, .vuelta-vuelos {{ margin-top: 20px; }}
                    .precio {{ font-weight: bold; color: #4CAF50; }}
                    .duracion {{ color: #777; }}
                    .aeropuerto {{ color: #555; font-size: 0.9em; }}
                    .back-btn {{ margin-top: 20px; padding: 10px; background: #4CAF50; color: white; text-decoration: none; display: inline-block; border-radius: 5px; }}
                </style>
            </head>
            <body>
                <h1>Vuelos encontrados</h1>
                <h2>De {origen_nombre} a {destino_nombre}</h2>
                <p>Fecha ida: {fecha_ida} - Fecha vuelta: {fecha_vuelta}</p>
                
                <div class="ida-vuelos">
                    <h2>Vuelos de ida</h2>
        '''
        
        for vuelo in vuelos_ida:
            html += f'''
                    <div class="vuelo-box">
                        <h3>{vuelo['aerolinea']} - Vuelo {vuelo['numero']}</h3>
                        <p>Fecha: {vuelo['fecha']}</p>
                        <p>Salida: {vuelo['hora_salida']} - Llegada: {vuelo['hora_llegada']}</p>
                        <p class="duracion">Duración: {vuelo['duracion_minutos'] // 60}h {vuelo['duracion_minutos'] % 60}min</p>
                        <p class="precio">Precio: {vuelo['precio']:.2f}€</p>
                        <p class="aeropuerto">Aeropuerto salida: {vuelo['aeropuerto_origen']['nombre']}</p>
                        <p class="aeropuerto">Aeropuerto llegada: {vuelo['aeropuerto_destino']['nombre']}</p>
                    </div>
            '''
        
        html += '''
                </div>
                
                <div class="vuelta-vuelos">
                    <h2>Vuelos de vuelta</h2>
        '''
        
        for vuelo in vuelos_vuelta:
            html += f'''
                    <div class="vuelo-box">
                        <h3>{vuelo['aerolinea']} - Vuelo {vuelo['numero']}</h3>
                        <p>Fecha: {vuelo['fecha']}</p>
                        <p>Salida: {vuelo['hora_salida']} - Llegada: {vuelo['hora_llegada']}</p>
                        <p class="duracion">Duración: {vuelo['duracion_minutos'] // 60}h {vuelo['duracion_minutos'] % 60}min</p>
                        <p class="precio">Precio: {vuelo['precio']:.2f}€</p>
                        <p class="aeropuerto">Aeropuerto salida: {vuelo['aeropuerto_origen']['nombre']}</p>
                        <p class="aeropuerto">Aeropuerto llegada: {vuelo['aeropuerto_destino']['nombre']}</p>
                    </div>
            '''
        
        html += '''
                </div>
                
                <a href="/test" class="back-btn">Volver a buscar</a>
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
    peticion_id = URIRef('peticion_transporte_' + str(uuid.uuid4()))
    g.add((peticion_id, RDF.type, onto.PeticionTransporte))
    
    # Origen y destino (por defecto Barcelona-Madrid)
    origen_param = request.args.get('origen', 'Barcelona')
    destino_param = request.args.get('destino', 'Madrid')
    
    # Fechas
    fecha_ida_param = request.args.get('fecha_ida', datetime.date.today().isoformat())
    fecha_vuelta_param = request.args.get('fecha_vuelta', (datetime.date.today() + datetime.timedelta(days=7)).isoformat())
    
    # Precio máximo
    precio_max_param = request.args.get('precio_max')
    
    # Crear nodo para origen
    origen_id = URIRef('ciudad_origen_' + str(uuid.uuid4()))
    g.add((origen_id, onto.NombreCiudad, Literal(origen_param)))
    g.add((peticion_id, onto.comoOrigen, origen_id))
    
    # Crear nodo para destino
    destino_id = URIRef('ciudad_destino_' + str(uuid.uuid4()))
    g.add((destino_id, onto.NombreCiudad, Literal(destino_param)))
    g.add((peticion_id, onto.comoDestino, destino_id))
    
    # Fechas
    g.add((peticion_id, onto.fecha_inicio, Literal(fecha_ida_param, datatype=XSD.date)))
    g.add((peticion_id, onto.fecha_fin, Literal(fecha_vuelta_param, datatype=XSD.date)))
    
    # Precio máximo si se ha indicado
    if precio_max_param:
        g.add((peticion_id, onto.PrecioMax, Literal(float(precio_max_param), datatype=XSD.float)))
    
    # Construir mensaje ACL
    msg = build_message(g, 
                        ACL.request,
                        sender=URIRef('http://test-sender'),
                        receiver=AgenteTransportes.uri,
                        content=peticion_id,
                        msgcnt=0)
    
    # Serializar el mensaje
    xml_msg = msg.serialize(format='xml')
    
    # Hacer la petición al agente
    import requests
    resp = requests.get(AgenteTransportes.address, params={'content': xml_msg})
    
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
            <h1>Prueba de Petición RDF al Agente de Transportes</h1>
            <p>Origen: <strong>{origen_param}</strong>, Destino: <strong>{destino_param}</strong></p>
            <p>Fecha ida: <strong>{fecha_ida_param}</strong>, Fecha vuelta: <strong>{fecha_vuelta_param}</strong></p>
            <p>Precio máximo: <strong>{precio_max_param if precio_max_param else 'No especificado'}</strong></p>
            
            <h2>Petición RDF enviada:</h2>
            <pre>{xml_msg.decode('utf-8')}</pre>
            
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


def consultar_dbpedia(uri):
    """
    Consulta información adicional sobre un recurso en DBpedia
    
    :param uri: URI del recurso a consultar
    :return: Diccionario con información adicional o None si hay error
    """
    try:
        if not str(uri).startswith('http://dbpedia.org/'):
            return None
            
        dbpedia_endpoint = "http://dbpedia.org/sparql"
        query = f"""
        PREFIX dbo: <http://dbpedia.org/ontology/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        
        SELECT ?label ?abstract ?country WHERE {{
            <{uri}> rdfs:label ?label .
            OPTIONAL {{ <{uri}> dbo:abstract ?abstract }}
            OPTIONAL {{ <{uri}> dbo:country ?country }}
            FILTER (lang(?label) = 'es' || lang(?label) = 'en')
            FILTER (lang(?abstract) = 'es' || lang(?abstract) = 'en')
        }}
        LIMIT 1
        """
        
        headers = {'Accept': 'application/json'}
        response = requests.get(dbpedia_endpoint, params={'query': query, 'format': 'json'}, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            results = data.get('results', {}).get('bindings', [])
            
            if results:
                result = results[0]
                return {
                    'label': result.get('label', {}).get('value', ''),
                    'abstract': result.get('abstract', {}).get('value', '')[:200] + '...' if 'abstract' in result else '',
                    'country': result.get('country', {}).get('value', '')
                }
        return None
    except Exception as e:
        logger.warning(f"Error al consultar DBpedia: {e}")
        return None

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
        logger.info('Agente de Transportes finalizado')
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals():
            ab1.terminate()
        print('Error en el Agente de Transportes')