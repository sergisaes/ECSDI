# -*- coding: utf-8 -*-
"""
*** Agente de Transportes ***

Este agente recibe peticiones para encontrar medios de transporte (principalmente vuelos)
entre origen y destino, y devuelve múltiples opciones.
Utiliza la API de Amadeus para obtener información de vuelos reales.

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
import json
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

agn = Namespace("http://www.agentes.org#")
onto = Namespace("http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2/")
am = Namespace("http://www.amadeus.com/")

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

amadeus = Client(
    client_id='w0fh6OZAxt6MlB8BSGUplHoIebgCco90',
    client_secret='bymSMBr9QNrCvTv0'
)

dsgraph = Graph()
try:
    dsgraph.parse("entrega2.ttl", format="turtle")
    logger.info("Ontología cargada correctamente")
except Exception as e:
    logger.error(f"Error al cargar la ontología: {e}")

transport_db = Graph()
transport_db.bind('am', am)
transport_db.bind('onto', onto)
transport_db.bind('xsd', XSD)

iata_to_city = {
    'BCN': 'Barcelona',
    'MAD': 'Madrid',
    'VLC': 'Valencia',
    'SVQ': 'Sevilla',
    'CDG': 'Paris',
    'FCO': 'Roma',
    'LHR': 'London',
    'TXL': 'Berlin',
    'AMS': 'Amsterdam',
    'LIS': 'Lisboa'
}

city_to_iata = {
    'Barcelona': 'BCN',
    'Madrid': 'MAD',
    'Valencia': 'VLC',
    'Sevilla': 'SVQ',
    'Paris': 'CDG',
    'Roma': 'FCO',
    'Londres': 'LHR',
    'Berlin': 'TXL',
    'Amsterdam': 'AMS',
    'Lisboa': 'LIS'
}

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

    if msgdic['performative'] == ACL.request:
        content = msgdic['content']
        
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionTransporte)):
            origen = None
            destino = None
            fecha_ida = None
            fecha_vuelta = None
            precio_max = None
            
            for s1, p1, o1 in gm.triples((s, onto.comoOrigen, None)):
                origen_nombre = None
                
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    origen_nombre = str(o2)
                
                if origen_nombre:
                    origen = origen_nombre
            
            for s1, p1, o1 in gm.triples((s, onto.comoDestino, None)):
                destino_nombre = None
                
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    destino_nombre = str(o2)
                
                if destino_nombre:
                    destino = destino_nombre
            
            for s1, p1, o1 in gm.triples((s, onto.fecha_inicio, None)):
                fecha_ida = str(o1)
            
            for s1, p1, o1 in gm.triples((s, onto.fecha_fin, None)):
                fecha_vuelta = str(o1)
            
            for s1, p1, o1 in gm.triples((s, onto.PrecioMax, None)):
                precio_max = float(o1)
            
            if origen and destino:
                respuesta = procesar_peticion_transporte(origen, destino, fecha_ida, fecha_vuelta, precio_max, content)
                return Response(respuesta, mimetype='text/xml')
            else:
                logger.warning("Petición incompleta: falta origen o destino")
                return Response(status=400)
    
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
    try:
        with open("transport_db.ttl", 'wb') as f:
            f.write(transport_db.serialize(format='turtle'))
        logger.info("Base de datos de transportes guardada correctamente")
    except Exception as e:
        logger.error(f"Error al guardar la base de datos: {e}")


def buscar_codigo_iata(ciudad):
    """
    Busca el código IATA para una ciudad utilizando la API de Amadeus
    
    :param ciudad: Nombre de la ciudad
    :return: Código IATA o None si no se encuentra
    """
    try:
        if ciudad in city_to_iata:
            return city_to_iata[ciudad]
        
        response = amadeus.reference_data.locations.get(
            keyword=ciudad,
            subType=["CITY"],
            page={'limit': 1}
        )
        
        if response.data:
            return response.data[0]['iataCode']
        
        return None
    except ResponseError as e:
        logger.error(f"Error al buscar código IATA para {ciudad}: {e}")
        return None


def buscar_vuelos_amadeus(origen_code, destino_code, fecha_ida, fecha_vuelta, precio_max=None):
   
    try:
        params = {
            'originLocationCode': origen_code,
            'destinationLocationCode': destino_code,
            'departureDate': fecha_ida,
            'adults': 1,
            'max': 10  
        }
        
        if fecha_vuelta:
            params['returnDate'] = fecha_vuelta
        
        if precio_max:
            params['maxPrice'] = int(precio_max)
        
        logger.info(f"Consultando vuelos: {origen_code} -> {destino_code}, ida: {fecha_ida}, vuelta: {fecha_vuelta}")
        response = amadeus.shopping.flight_offers_search.get(**params)
        
        vuelos_ida = []
        vuelos_vuelta = []
        
        for offer in response.data:
            price = float(offer['price']['grandTotal'])
            
            for itinerary_idx, itinerary in enumerate(offer['itineraries']):
                is_outbound = itinerary_idx == 0  
                
                segments = itinerary['segments']
                departure = segments[0]['departure']
                arrival = segments[-1]['arrival']
                
                vuelo_info = {
                    'id': offer['id'],
                    'precio': price / len(offer['itineraries']),  
                    'fecha': departure['at'].split('T')[0],
                    'hora_salida': departure['at'].split('T')[1].split('.')[0][:5],
                    'hora_llegada': arrival['at'].split('T')[1].split('.')[0][:5],
                    'aeropuerto_origen': {
                        'code': departure['iataCode'],
                        'nombre': iata_to_city.get(departure['iataCode'], departure['iataCode'])
                    },
                    'aeropuerto_destino': {
                        'code': arrival['iataCode'],
                        'nombre': iata_to_city.get(arrival['iataCode'], arrival['iataCode'])
                    },
                    'aerolinea': segments[0]['carrierCode'],
                    'numero': segments[0]['number'],
                    'duracion_minutos': calcular_duracion_minutos(itinerary['duration']),
                    'tipo': 'ida' if is_outbound else 'vuelta',
                    'escala': len(segments) > 1,
                    'num_escalas': len(segments) - 1,
                    'segments': segments  
                }
                
                if is_outbound:
                    vuelos_ida.append(vuelo_info)
                else:
                    vuelos_vuelta.append(vuelo_info)
        
        return vuelos_ida, vuelos_vuelta
        
    except ResponseError as e:
        logger.error(f"Error al buscar vuelos: {e}")
        return [], []
    except Exception as e:
        logger.error(f"Error inesperado al buscar vuelos: {e}")
        return [], []


def calcular_duracion_minutos(duracion_str):

    try:
        duracion = duracion_str.replace('PT', '')
        
        horas = 0
        minutos = 0
        
        if 'H' in duracion:
            h_parts = duracion.split('H')
            horas = int(h_parts[0])
            duracion = h_parts[1]
        
        if 'M' in duracion:
            m_parts = duracion.split('M')
            minutos = int(m_parts[0])
        
        return horas * 60 + minutos
    except Exception:
        return 80  




def eliminar_duplicados(vuelos):
    resultado = []
    vuelos_vistos = set()  
    
    for vuelo in vuelos:
        id_vuelo = f"{vuelo['aerolinea']}-{vuelo['numero']}-{vuelo['fecha']}-{vuelo['hora_salida']}"
        
        if id_vuelo not in vuelos_vistos:
            vuelos_vistos.add(id_vuelo)
            resultado.append(vuelo)
    
    return resultado


def procesar_peticion_transporte(origen, destino, fecha_ida, fecha_vuelta, precio_max, content_uri):
   
    global mss_cnt
    
    logger.info(f"Buscando transporte desde {origen} hacia {destino}")
    
    origen_code = buscar_codigo_iata(origen)
    destino_code = buscar_codigo_iata(destino)
    
    if not origen_code or not destino_code:
        logger.warning(f"No se encontraron códigos IATA para {origen} o {destino}")
        origen_code = city_to_iata.get(origen, origen[:3].upper())
        destino_code = city_to_iata.get(destino, destino[:3].upper())
    
    vuelos_ida, vuelos_vuelta = buscar_vuelos_amadeus(origen_code, destino_code, fecha_ida, fecha_vuelta, precio_max)
    
    if precio_max is not None:
        vuelos_ida = [v for v in vuelos_ida if v['precio'] <= precio_max / 2]
        vuelos_vuelta = [v for v in vuelos_vuelta if v['precio'] <= precio_max / 2]
    
    vuelos_ida = vuelos_ida[:5]
    vuelos_vuelta = vuelos_vuelta[:5]
    
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    g.bind('am', am)
    
    respuesta_id = URIRef(f'respuesta_transporte_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaTransporte))
    
    for vuelo in vuelos_ida:
        vuelo_id = URIRef(f"vuelo_ida_{str(uuid.uuid4())}")
        g.add((vuelo_id, RDF.type, onto.Avion)) 
        g.add((respuesta_id, onto.formadoPorTransportes, vuelo_id))
        
        g.add((vuelo_id, onto.Precio, Literal(vuelo['precio'], datatype=XSD.float)))
        aerolinea_nombre = vuelo.get('aerolinea_nombre', vuelo['aerolinea'])
        g.add((vuelo_id, RDFS.label, Literal(f"Vuelo {vuelo['numero']} - {aerolinea_nombre}")))
        g.add((vuelo_id, onto.Salida, Literal(f"{vuelo['fecha']}T{vuelo['hora_salida']}:00", datatype=XSD.dateTime)))
        g.add((vuelo_id, onto.Llegada, Literal(f"{vuelo['fecha']}T{vuelo['hora_llegada']}:00", datatype=XSD.dateTime)))
        
        g.add((vuelo_id, onto.duracionMinutos, Literal(vuelo['duracion_minutos'], datatype=XSD.integer)))
        
        origen_uri = URIRef(f"http://www.amadeus.com/airport/{vuelo['aeropuerto_origen']['code']}")
        destino_uri = URIRef(f"http://www.amadeus.com/airport/{vuelo['aeropuerto_destino']['code']}")
        
        g.add((vuelo_id, onto.saleDe, origen_uri))
        g.add((vuelo_id, onto.llegaA, destino_uri))
        
        g.add((origen_uri, RDF.type, onto.Aeropuerto))
        g.add((origen_uri, RDFS.label, Literal(vuelo['aeropuerto_origen']['nombre'])))
        g.add((origen_uri, am.iataCode, Literal(vuelo['aeropuerto_origen']['code'])))
        
        g.add((destino_uri, RDF.type, onto.Aeropuerto))
        g.add((destino_uri, RDFS.label, Literal(vuelo['aeropuerto_destino']['nombre'])))
        g.add((destino_uri, am.iataCode, Literal(vuelo['aeropuerto_destino']['code'])))
        
        aerolinea_uri = URIRef(f"http://www.amadeus.com/airline/{vuelo['aerolinea']}")
        g.add((vuelo_id, onto.operadoPor, aerolinea_uri))
        g.add((aerolinea_uri, RDF.type, onto.Aerolinea))
        g.add((aerolinea_uri, RDFS.label, Literal(aerolinea_nombre)))
        g.add((aerolinea_uri, am.code, Literal(vuelo['aerolinea'])))
        
        g.add((vuelo_id, RDFS.comment, Literal(f"Vuelo operado por {aerolinea_nombre}")))
        g.add((vuelo_id, onto.IdVuelo, Literal(vuelo['numero'])))
        
        g.add((vuelo_id, am.numEscalas, Literal(vuelo.get('num_escalas', 0), datatype=XSD.integer)))
        g.add((vuelo_id, am.tieneEscalas, Literal(vuelo.get('escala', False), datatype=XSD.boolean)))
    
    for vuelo in vuelos_vuelta:
        vuelo_id = URIRef(f"vuelo_vuelta_{str(uuid.uuid4())}")
        g.add((vuelo_id, RDF.type, onto.Avion))
        g.add((respuesta_id, onto.formadoPorTransportes, vuelo_id))
        
        g.add((vuelo_id, onto.Precio, Literal(vuelo['precio'], datatype=XSD.float)))
        aerolinea_nombre = vuelo.get('aerolinea_nombre', vuelo['aerolinea'])
        g.add((vuelo_id, RDFS.label, Literal(f"Vuelo {vuelo['numero']} - {aerolinea_nombre}")))
        g.add((vuelo_id, onto.Salida, Literal(f"{vuelo['fecha']}T{vuelo['hora_salida']}:00", datatype=XSD.dateTime)))
        g.add((vuelo_id, onto.Llegada, Literal(f"{vuelo['fecha']}T{vuelo['hora_llegada']}:00", datatype=XSD.dateTime)))
        
        g.add((vuelo_id, onto.duracionMinutos, Literal(vuelo['duracion_minutos'], datatype=XSD.integer)))
        
        origen_uri = URIRef(f"http://www.amadeus.com/airport/{vuelo['aeropuerto_origen']['code']}")
        destino_uri = URIRef(f"http://www.amadeus.com/airport/{vuelo['aeropuerto_destino']['code']}")
        
        g.add((vuelo_id, onto.saleDe, origen_uri))
        g.add((vuelo_id, onto.llegaA, destino_uri))
        
        g.add((origen_uri, RDF.type, onto.Aeropuerto))
        g.add((origen_uri, RDFS.label, Literal(vuelo['aeropuerto_origen']['nombre'])))
        g.add((origen_uri, am.iataCode, Literal(vuelo['aeropuerto_origen']['code'])))
        
        g.add((destino_uri, RDF.type, onto.Aeropuerto))
        g.add((destino_uri, RDFS.label, Literal(vuelo['aeropuerto_destino']['nombre'])))
        g.add((destino_uri, am.iataCode, Literal(vuelo['aeropuerto_destino']['code'])))
        
        aerolinea_uri = URIRef(f"http://www.amadeus.com/airline/{vuelo['aerolinea']}")
        g.add((vuelo_id, onto.operadoPor, aerolinea_uri))
        g.add((aerolinea_uri, RDF.type, onto.Aerolinea))
        g.add((aerolinea_uri, RDFS.label, Literal(aerolinea_nombre)))
        g.add((aerolinea_uri, am.code, Literal(vuelo['aerolinea'])))
        
        g.add((vuelo_id, RDFS.comment, Literal(f"Vuelo operado por {aerolinea_nombre}")))
        g.add((vuelo_id, onto.IdVuelo, Literal(vuelo['numero'])))
        
        g.add((vuelo_id, am.numEscalas, Literal(vuelo.get('num_escalas', 0), datatype=XSD.integer)))
        g.add((vuelo_id, am.tieneEscalas, Literal(vuelo.get('escala', False), datatype=XSD.boolean)))
    
    respuesta_uri = URIRef(f"respuesta_{str(uuid.uuid4())}")
    transport_db.add((respuesta_uri, RDF.type, onto.RespuestaTransporte))
    transport_db.add((respuesta_uri, RDFS.label, Literal(f"Transportes de {origen} a {destino}")))
    transport_db.add((respuesta_uri, RDFS.comment, Literal(f"Búsqueda realizada el {datetime.datetime.now().isoformat()}")))
    
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
    gmess = Graph()
    gmess.bind('foaf', FOAF)
    gmess.bind('dso', DSO)
    reg_obj = agn[AgenteTransportes.name + '-Register']
    gmess.add((reg_obj, RDF.type, DSO.Register))
    gmess.add((reg_obj, DSO.Uri, AgenteTransportes.uri))
    gmess.add((reg_obj, FOAF.name, Literal(AgenteTransportes.name)))
    gmess.add((reg_obj, DSO.Address, Literal(AgenteTransportes.address)))
    gmess.add((reg_obj, DSO.AgentType, DSO.TransportAgent))

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
    
    while True:
        try:
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
        return '''
        <html>
            <head>
                <title>Test Agente Transportes con Amadeus</title>
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
                <h1>Test Agente Transportes con API Amadeus</h1>
                
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
                        <input type="date" name="fecha_vuelta">
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
        origen = request.form['origen']
        destino = request.form['destino']
        fecha_ida = request.form['fecha_ida']
        fecha_vuelta = request.form.get('fecha_vuelta')
        precio_max = request.form.get('precio_max')
        
        if precio_max:
            precio_max = float(precio_max)
        
        origen_code = buscar_codigo_iata(origen)
        destino_code = buscar_codigo_iata(destino)
        
        vuelos_ida, vuelos_vuelta = buscar_vuelos_amadeus(origen_code, destino_code, fecha_ida, fecha_vuelta, precio_max)
        
        
        vuelos_ida = eliminar_duplicados(vuelos_ida)
        vuelos_vuelta = eliminar_duplicados(vuelos_vuelta) if vuelos_vuelta else []
        
        if precio_max is not None:
            vuelos_ida = [v for v in vuelos_ida if v['precio'] <= precio_max / 2]
            vuelos_vuelta = [v for v in vuelos_vuelta if v['precio'] <= precio_max / 2]
        
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
                    .ficticio {{ color: #f44336; font-style: italic; }}
                    .escalas {{ color: #2196F3; }}
                </style>
            </head>
            <body>
                <h1>Vuelos encontrados</h1>
                <h2>De {origen} ({origen_code}) a {destino} ({destino_code})</h2>
                <p>Fecha ida: {fecha_ida}{' - Fecha vuelta: ' + fecha_vuelta if fecha_vuelta else ''}</p>
                
                <div class="ida-vuelos">
                    <h2>Vuelos de ida</h2>
        '''
        
        for vuelo in vuelos_ida:
            aerolinea = vuelo.get('aerolinea_nombre', vuelo['aerolinea'])
            es_ficticio = vuelo.get('ficticio', False)
            
            html += f'''
                    <div class="vuelo-box">
                        <h3>{aerolinea} - Vuelo {vuelo['numero']}</h3>
                        {"<span class='ficticio'>(Datos simulados)</span>" if es_ficticio else ""}
                        <p>Fecha: {vuelo['fecha']}</p>
                        <p>Salida: {vuelo['hora_salida']} - Llegada: {vuelo['hora_llegada']}</p>
                        <p class="duracion">Duración: {vuelo['duracion_minutos'] // 60}h {vuelo['duracion_minutos'] % 60}min</p>
                        <p class="precio">Precio: {vuelo['precio']:.2f}€</p>
                        <p class="aeropuerto">Aeropuerto salida: {vuelo['aeropuerto_origen']['nombre']} ({vuelo['aeropuerto_origen']['code']})</p>
                        <p class="aeropuerto">Aeropuerto llegada: {vuelo['aeropuerto_destino']['nombre']} ({vuelo['aeropuerto_destino']['code']})</p>
                        <p class="escalas">{"Vuelo con " + str(vuelo.get('num_escalas', 0)) + " escalas" if vuelo.get('escala', False) else "Vuelo directo"}</p>
                    </div>
            '''
        
        if fecha_vuelta:
            html += '''
                    </div>
                    
                    <div class="vuelta-vuelos">
                        <h2>Vuelos de vuelta</h2>
            '''
            
            for vuelo in vuelos_vuelta:
                aerolinea = vuelo.get('aerolinea_nombre', vuelo['aerolinea'])
                es_ficticio = vuelo.get('ficticio', False)
                
                html += f'''
                        <div class="vuelo-box">
                            <h3>{aerolinea} - Vuelo {vuelo['numero']}</h3>
                            {"<span class='ficticio'>(Datos simulados)</span>" if es_ficticio else ""}
                            <p>Fecha: {vuelo['fecha']}</p>
                            <p>Salida: {vuelo['hora_salida']} - Llegada: {vuelo['hora_llegada']}</p>
                            <p class="duracion">Duración: {vuelo['duracion_minutos'] // 60}h {vuelo['duracion_minutos'] % 60}min</p>
                            <p class="precio">Precio: {vuelo['precio']:.2f}€</p>
                            <p class="aeropuerto">Aeropuerto salida: {vuelo['aeropuerto_origen']['nombre']} ({vuelo['aeropuerto_origen']['code']})</p>
                            <p class="aeropuerto">Aeropuerto llegada: {vuelo['aeropuerto_destino']['nombre']} ({vuelo['aeropuerto_destino']['code']})</p>
                            <p class="escalas">{"Vuelo con " + str(vuelo.get('num_escalas', 0)) + " escalas" if vuelo.get('escala', False) else "Vuelo directo"}</p>
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
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('onto', onto)
    g.bind('agn', agn)
    
    peticion_id = URIRef('peticion_transporte_' + str(uuid.uuid4()))
    g.add((peticion_id, RDF.type, onto.PeticionTransporte))
    
    origen_param = request.args.get('origen', 'Barcelona')
    destino_param = request.args.get('destino', 'Madrid')
    
    fecha_ida_param = request.args.get('fecha_ida', datetime.date.today().isoformat())
    fecha_vuelta_param = request.args.get('fecha_vuelta', (datetime.date.today() + datetime.timedelta(days=7)).isoformat())
    
    precio_max_param = request.args.get('precio_max')
    
    origen_id = URIRef('ciudad_origen_' + str(uuid.uuid4()))
    g.add((origen_id, onto.NombreCiudad, Literal(origen_param)))
    g.add((peticion_id, onto.comoOrigen, origen_id))
    
    destino_id = URIRef('ciudad_destino_' + str(uuid.uuid4()))
    g.add((destino_id, onto.NombreCiudad, Literal(destino_param)))
    g.add((peticion_id, onto.comoDestino, destino_id))
    
    g.add((peticion_id, onto.fecha_inicio, Literal(fecha_ida_param, datatype=XSD.date)))
    
    if fecha_vuelta_param:
        g.add((peticion_id, onto.fecha_fin, Literal(fecha_vuelta_param, datatype=XSD.date)))
    
    if precio_max_param:
        g.add((peticion_id, onto.PrecioMax, Literal(float(precio_max_param), datatype=XSD.float)))
    
    msg = build_message(g, 
                        ACL.request,
                        sender=URIRef('http://test-sender'),
                        receiver=AgenteTransportes.uri,
                        content=peticion_id,
                        msgcnt=0)
    
    xml_msg = msg.serialize(format='xml')
    
    import requests
    
    # Modificación: usar localhost en lugar de 0.0.0.0
    endpoint_url = f'http://127.0.0.1:{port}/comm'
    
    try:
        logger.info(f"Enviando petición de prueba a {endpoint_url}")
        resp = requests.get(endpoint_url, params={'content': xml_msg})
        response_text = resp.text if resp.status_code == 200 else "Error en la petición"
        response_status = resp.status_code
    except Exception as e:
        logger.error(f"Error en la conexión: {e}")
        response_text = f"Error en la conexión: {str(e)}"
        response_status = 500
    
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
            <h1>Prueba de Petición RDF al Agente de Transportes (API Amadeus)</h1>
            <p>Origen: <strong>{origen_param}</strong>, Destino: <strong>{destino_param}</strong></p>
            <p>Fecha ida: <strong>{fecha_ida_param}</strong>, Fecha vuelta: <strong>{fecha_vuelta_param if fecha_vuelta_param else 'No especificada'}</strong></p>
            <p>Precio máximo: <strong>{precio_max_param if precio_max_param else 'No especificado'}</strong></p>
            
            <h2>Petición RDF enviada:</h2>
            <pre>{xml_msg.decode('utf-8')}</pre>
            
            <h2>Estado de la respuesta: 
                <span class="{'success' if response_status == 200 else 'error'}">
                    {response_status}
                </span>
            </h2>
            
            <h2>Respuesta recibida:</h2>
            <pre>{response_text}</pre>
            
            <p><a href="/test">Volver al formulario de pruebas</a></p>
        </body>
    </html>
    '''
    return html



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
        logger.info('Agente de Transportes finalizado')
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals():
            ab1.terminate()
        print('Error en el Agente de Transportes')