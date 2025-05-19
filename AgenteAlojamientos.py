# -*- coding: utf-8 -*-
"""
Agente de Alojamientos
Responsable de buscar y ofrecer opciones de alojamiento usando API Amadeus

@author: Arnau
"""

from multiprocessing import Process, Queue
import socket
import argparse
import datetime
import uuid
import random
from amadeus import Client, ResponseError

from rdflib import Namespace, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD, FOAF
from flask import Flask, request, Response

from AgentUtil.FlaskServer import shutdown_server
from AgentUtil.Agent import Agent
from AgentUtil.ACLMessages import build_message, send_message, get_message_properties
from AgentUtil.ACL import ACL
from AgentUtil.DSO import DSO

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

__author__ = 'Arnau'

# Configuración
parser = argparse.ArgumentParser()
parser.add_argument('--open', help="Define si el servidor está abierto al exterior o no", action='store_true', default=False)
parser.add_argument('--port', type=int, help="Puerto de comunicación del agente")
parser.add_argument('--dhost', help="Host del agente de directorio")
parser.add_argument('--dport', type=int, help="Puerto del agente de directorio")
args = parser.parse_args()

# Configuración del host y puerto
port = args.port if args.port else 9002
hostname = '0.0.0.0' if args.open else socket.gethostname()
dhostname = args.dhost if args.dhost else socket.gethostname()
dport = args.dport if args.dport else 9000

# Namespaces
agn = Namespace("http://www.agentes.org#")
onto = Namespace("http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2/")
am = Namespace("http://www.amadeus.com/")

# Contador de mensajes
mss_cnt = 0

# Datos del Agente
AgenteAlojamientos = Agent('AgenteAlojamientos',
                        agn.AgenteAlojamientos,
                        'http://%s:%d/comm' % (hostname, port),
                        'http://%s:%d/Stop' % (hostname, port))

# Directory agent address
DirectoryAgent = Agent('DirectoryAgent',
                       agn.Directory,
                       'http://%s:%d/Register' % (dhostname, dport),
                       'http://%s:%d/Stop' % (dhostname, dport))

# Cliente Amadeus
amadeus = Client(
    client_id='w0fh6OZAxt6MlB8BSGUplHoIebgCco90',
    client_secret='bymSMBr9QNrCvTv0'
)

# Cargar ontología y preparar base de datos
dsgraph = Graph()
try:
    dsgraph.parse("entrega2.ttl", format="turtle")
except Exception as e:
    logger.error(f"Error al cargar la ontología: {e}")

# Base de datos de alojamientos
alojamientos_db = Graph()
alojamientos_db.bind('am', am)
alojamientos_db.bind('onto', onto)
alojamientos_db.bind('xsd', XSD)

# Mapeo de códigos IATA de ciudades
city_to_iata = {
    'Barcelona': 'BCN', 'Madrid': 'MAD', 'Valencia': 'VLC', 'Sevilla': 'SVQ',
    'Paris': 'PAR', 'Roma': 'ROM', 'Londres': 'LON', 'Berlin': 'BER', 
    'Amsterdam': 'AMS', 'Lisboa': 'LIS'
}
iata_to_city = {v: k for k, v in city_to_iata.items()}

cola1 = Queue()
app = Flask(__name__)


@app.route("/comm")
def comunicacion():
    """Punto de entrada de comunicación para recibir peticiones de alojamiento"""
    global dsgraph, mss_cnt

    message = request.args['content']
    gm = Graph()
    gm.parse(data=message, format='xml')
    
    msgdic = get_message_properties(gm)

    if msgdic['performative'] == ACL.request:
        content = msgdic['content']
        
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionAlojamiento)):
            ciudad = None
            fecha_entrada = None
            fecha_salida = None
            precio_max = None
            radio_alojamiento = None
            
            # Extraer datos de la petición
            for s1, p1, o1 in gm.triples((s, onto.comoRestriccionLocalidad, None)):
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    ciudad = str(o2)
            
            for s1, p1, o1 in gm.triples((s, onto.fecha_inicio, None)):
                fecha_entrada = str(o1)
            
            for s1, p1, o1 in gm.triples((s, onto.fecha_fin, None)):
                fecha_salida = str(o1)
            
            for s1, p1, o1 in gm.triples((s, onto.PrecioMax, None)):
                precio_max = float(o1)
            
            for s1, p1, o1 in gm.triples((s, onto.RadioAlojamiento, None)):
                radio_alojamiento = float(o1)
            
            if ciudad:
                respuesta = procesar_peticion_alojamiento(ciudad, fecha_entrada, fecha_salida, precio_max, radio_alojamiento, content)
                return Response(respuesta, mimetype='text/xml')
            else:
                return Response(status=400)
    
    return Response(status=400)


@app.route("/Stop")
def stop():
    """Detiene el agente"""
    tidyup()
    shutdown_server()
    return "Parando Agente de Alojamientos"


def tidyup():
    """Limpieza antes de detener el agente"""
    global cola1
    cola1.put(0)
    try:
        with open("alojamientos_db.ttl", 'wb') as f:
            f.write(alojamientos_db.serialize(format='turtle'))
    except Exception as e:
        logger.error(f"Error al guardar la base de datos: {e}")


def buscar_codigo_ciudad(ciudad):
    """Busca el código IATA de una ciudad"""
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
    except Exception:
        return None


def buscar_hoteles_amadeus(ciudad_code, fecha_entrada, fecha_salida, precio_max=None, radio=20):
    """Busca hoteles para una ciudad y fechas específicas"""
    try:
        # Validar fechas
        fecha_actual = datetime.datetime.now()
        fecha_entrada_obj = datetime.datetime.strptime(fecha_entrada, '%Y-%m-%d')
        fecha_salida_obj = datetime.datetime.strptime(fecha_salida, '%Y-%m-%d')
        
        # Ajustar fechas demasiado lejanas
        max_future = fecha_actual + datetime.timedelta(days=365)
        if fecha_entrada_obj > max_future:
            nueva_fecha = fecha_actual + datetime.timedelta(days=30)
            fecha_entrada = nueva_fecha.strftime('%Y-%m-%d')
            fecha_salida = (nueva_fecha + datetime.timedelta(days=3)).strftime('%Y-%m-%d')
            fecha_entrada_obj = datetime.datetime.strptime(fecha_entrada, '%Y-%m-%d')
            fecha_salida_obj = datetime.datetime.strptime(fecha_salida, '%Y-%m-%d')
        
        # Obtener lista de hoteles por ciudad
        try:
            hoteles_response = amadeus.reference_data.locations.hotels.by_city.get(
                cityCode=ciudad_code
            )
            
            # Procesar los hoteles encontrados (máximo 10)
            hoteles_muestra = hoteles_response.data[:10]
            hoteles = []
            
            for hotel_data in hoteles_muestra:
                try:
                    hotel_id = hotel_data.get('hotelId')
                    hotel_name = hotel_data.get('name', 'Hotel sin nombre')
                    
                    # Simulamos precio realista
                    precio_base = random.uniform(80, 250)
                    dias = max(1, (fecha_salida_obj - fecha_entrada_obj).days)
                    precio_total = precio_base * dias
                    
                    # Verificar restricción de precio
                    if precio_max and precio_base > precio_max:
                        continue
                    
                    hotel_info = {
                        'id': hotel_id,
                        'nombre': hotel_name,
                        'categoria': str(random.choice([3, 4, 5])),
                        'precio_noche': precio_base,
                        'precio_total': precio_total,
                        'direccion': f"Dirección en {ciudad_code}",
                        'ciudad': iata_to_city.get(ciudad_code, ciudad_code),
                        'codigo_postal': f"{random.randint(10000, 99999)}",
                        'amenities': random.sample(["WIFI", "POOL", "PARKING", "RESTAURANT", "FITNESS_CENTER"], 3),
                        'fecha_entrada': fecha_entrada,
                        'fecha_salida': fecha_salida,
                        'disponibilidad': "Disponible",
                        'ficticio': False,
                        'semi_ficticio': True
                    }
                    hoteles.append(hotel_info)
                    
                except Exception:
                    pass
            
            # Complementar con ficticios si es necesario
            if len(hoteles) < 3:
                hoteles.extend(generar_hoteles_ficticios(ciudad_code, fecha_entrada, fecha_salida, precio_max, 5 - len(hoteles)))
            
            return sorted(hoteles, key=lambda x: x['precio_noche'])[:5]
            
        except Exception:
            return generar_hoteles_ficticios(ciudad_code, fecha_entrada, fecha_salida, precio_max, 5)
        
    except Exception:
        return generar_hoteles_ficticios(ciudad_code, fecha_entrada, fecha_salida, precio_max, 5)


def generar_hoteles_ficticios(ciudad_code, fecha_entrada, fecha_salida, precio_max=None, cantidad=5):
    """Genera datos ficticios de hoteles"""
    ciudad_nombre = iata_to_city.get(ciudad_code, ciudad_code)
    
    nombres_hoteles = [
        f"Hotel {ciudad_nombre} Central",
        f"Grand Hotel {ciudad_nombre}",
        f"Hotel Plaza {ciudad_nombre}",
        f"{ciudad_nombre} Premium Suites",
        f"Royal {ciudad_nombre} Resort"
    ]
    
    categorias = [3, 4, 5, 3, 4]
    amenities = [
        "POOL", "FITNESS_CENTER", "SPA", "RESTAURANT", "PARKING",
        "BUSINESS_CENTER", "CONFERENCE_ROOM", "WIFI", "ROOM_SERVICE"
    ]
    
    hoteles = []
    
    for i in range(cantidad):
        # Generar precio aleatorio
        precio_base = random.uniform(50, 200)
        if precio_max:
            precio_base = min(precio_base, precio_max * 0.9)
            
        dias = max(1, (datetime.datetime.strptime(fecha_salida, '%Y-%m-%d') - 
                      datetime.datetime.strptime(fecha_entrada, '%Y-%m-%d')).days)
        precio_total = precio_base * dias
        
        hotel_info = {
            'id': f"FICTICIO-{uuid.uuid4()}",
            'nombre': random.choice(nombres_hoteles),
            'categoria': str(random.choice(categorias)),
            'precio_noche': precio_base,
            'precio_total': precio_total,
            'direccion': f"Avenida Principal {random.randint(1, 200)}, {ciudad_nombre}",
            'ciudad': ciudad_nombre,
            'codigo_postal': f"{random.randint(10000, 99999)}",
            'latitud': "40.416775",
            'longitud': "-3.703790",
            'descripcion': f"Cómodo hotel en {ciudad_nombre}.",
            'fecha_entrada': fecha_entrada,
            'fecha_salida': fecha_salida,
            'amenities': random.sample(amenities, random.randint(3, 5)),
            'disponibilidad': "Disponible",
            'ficticio': True
        }
        
        hoteles.append(hotel_info)
    
    return hoteles


def procesar_peticion_alojamiento(ciudad, fecha_entrada, fecha_salida, precio_max, radio_alojamiento, content_uri):
    """Procesa una petición de alojamiento y construye la respuesta RDF"""
    global mss_cnt
    
    ciudad_code = buscar_codigo_ciudad(ciudad)
    
    if not ciudad_code:
        ciudad_code = ciudad[:3].upper()
    
    # Radio predeterminado si no se especifica
    radio = radio_alojamiento if radio_alojamiento else 15
    
    # Buscar hoteles
    hoteles = buscar_hoteles_amadeus(ciudad_code, fecha_entrada, fecha_salida, precio_max, radio)
    
    # Crear el grafo de respuesta
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    g.bind('am', am)
    
    # Crear respuesta
    respuesta_id = URIRef(f'respuesta_alojamiento_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaAlojamiento))
    
    # Añadir cada hotel a la respuesta
    for hotel in hoteles:
        hotel_id = URIRef(f"hotel_{hotel['id']}_{str(uuid.uuid4())}")
        g.add((hotel_id, RDF.type, onto.Hotel))
        g.add((respuesta_id, onto.formadoPorAlojamientos, hotel_id))
        
        # Información básica del hotel
        g.add((hotel_id, onto.Precio, Literal(hotel['precio_noche'], datatype=XSD.float)))
        g.add((hotel_id, RDFS.label, Literal(hotel['nombre'])))
        g.add((hotel_id, onto.PrecioTotal, Literal(hotel['precio_total'], datatype=XSD.float)))
        
        # Fechas de estancia
        g.add((hotel_id, onto.Salida, Literal(fecha_entrada, datatype=XSD.date)))
        g.add((hotel_id, onto.Llegada, Literal(fecha_salida, datatype=XSD.date)))
        
        # Ubicación
        if 'latitud' in hotel and 'longitud' in hotel:
            g.add((hotel_id, onto.Ubicacion, Literal(f"{hotel['latitud']},{hotel['longitud']}")))
        
        # Dirección
        g.add((hotel_id, RDFS.comment, Literal(f"{hotel['direccion']}, {hotel['ciudad']}")))
        
        # Categoría
        g.add((hotel_id, onto.Categoria, Literal(hotel['categoria'], datatype=XSD.integer)))
        
        # Servicios
        for amenity in hotel.get('amenities', []):
            amenity_uri = URIRef(f"amenity_{str(uuid.uuid4())}")
            g.add((amenity_uri, RDF.type, onto.Servicio))
            g.add((amenity_uri, RDFS.label, Literal(amenity)))
            g.add((hotel_id, onto.tieneServicio, amenity_uri))
        
        # Marcar si es ficticio
        if hotel.get('ficticio', False):
            g.add((hotel_id, am.esFicticio, Literal(True, datatype=XSD.boolean)))
    
    # Guardar información en la base de datos local
    respuesta_uri = URIRef(f"respuesta_alojamiento_db_{str(uuid.uuid4())}")
    alojamientos_db.add((respuesta_uri, RDF.type, onto.RespuestaAlojamiento))
    alojamientos_db.add((respuesta_uri, RDFS.label, Literal(f"Alojamientos en {ciudad}")))
    
    # Construir el mensaje de respuesta
    mss_cnt += 1
    return build_message(g, ACL.inform,
                        sender=AgenteAlojamientos.uri,
                        receiver=content_uri,
                        msgcnt=mss_cnt).serialize(format='xml')


def agentbehavior1(cola):
    """Comportamiento del agente - Registrarse en el directorio"""
    global mss_cnt
    gmess = Graph()
    gmess.bind('foaf', FOAF)
    gmess.bind('dso', DSO)
    reg_obj = agn[AgenteAlojamientos.name + '-Register']
    gmess.add((reg_obj, RDF.type, DSO.Register))
    gmess.add((reg_obj, DSO.Uri, AgenteAlojamientos.uri))
    gmess.add((reg_obj, FOAF.name, Literal(AgenteAlojamientos.name)))
    gmess.add((reg_obj, DSO.Address, Literal(AgenteAlojamientos.address)))
    gmess.add((reg_obj, DSO.AgentType, DSO.HotelsAgent))

    try:
        send_message(
            build_message(gmess, ACL.request,
                        sender=AgenteAlojamientos.uri,
                        receiver=DirectoryAgent.uri,
                        content=reg_obj,
                        msgcnt=mss_cnt),
            DirectoryAgent.address
        )
        mss_cnt += 1
    except Exception:
        pass
    
    while True:
        try:
            msg = cola.get()
            if msg == 0:
                break
        except Exception:
            break


@app.route("/test", methods=['GET', 'POST'])
def test_interface():
    """Interfaz web para pruebas"""
    if request.method == 'GET':
        return '''
        <html>
            <head>
                <title>Test Agente Alojamientos</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; }
                    .form-group { margin-bottom: 15px; }
                    label { display: block; margin-bottom: 5px; }
                    input, select { padding: 8px; width: 300px; }
                    button { padding: 10px 15px; background-color: #4CAF50; color: white; border: none; cursor: pointer; }
                </style>
            </head>
            <body>
                <h1>Test Agente Alojamientos</h1>
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
                        <label>Fecha entrada:</label>
                        <input type="date" name="fecha_entrada" required>
                    </div>
                    <div class="form-group">
                        <label>Fecha salida:</label>
                        <input type="date" name="fecha_salida" required>
                    </div>
                    <div class="form-group">
                        <label>Precio máximo por noche (€):</label>
                        <input type="number" name="precio_max" min="1" step="1">
                    </div>
                    <div class="form-group">
                        <label>Radio de búsqueda (km):</label>
                        <input type="number" name="radio" min="1" max="50" value="15">
                    </div>
                    <button type="submit">Buscar alojamientos</button>
                </form>
            </body>
        </html>
        '''
    else:
        ciudad = request.form['ciudad']
        fecha_entrada = request.form['fecha_entrada']
        fecha_salida = request.form['fecha_salida']
        precio_max = request.form.get('precio_max')
        radio = request.form.get('radio')
        
        if precio_max:
            precio_max = float(precio_max)
        
        if radio:
            radio = float(radio)
        
        ciudad_code = buscar_codigo_ciudad(ciudad)
        hoteles = buscar_hoteles_amadeus(ciudad_code, fecha_entrada, fecha_salida, precio_max, radio)
        
        html = f'''
        <html>
            <head>
                <title>Resultados de búsqueda de alojamientos</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .hotel-box {{ background: #f9f9f9; padding: 15px; margin: 10px 0; border-radius: 5px; }}
                    .precio {{ font-weight: bold; color: #4CAF50; }}
                    .categoria {{ color: #FF9800; }}
                    .back-btn {{ margin-top: 20px; padding: 10px; background: #4CAF50; color: white; text-decoration: none; display: inline-block; border-radius: 5px; }}
                    .ficticio {{ color: #f44336; font-style: italic; }}
                </style>
            </head>
            <body>
                <h1>Alojamientos encontrados en {ciudad}</h1>
                <p>Estancia: {fecha_entrada} al {fecha_salida}</p>
        '''
        
        for hotel in hoteles:
            estrellas = "★" * int(hotel['categoria'])
            es_ficticio = hotel.get('ficticio', False)
            
            html += f'''
                <div class="hotel-box">
                    <h3>{hotel['nombre']}</h3>
                    {"<span class='ficticio'>(Datos simulados)</span>" if es_ficticio else ""}
                    <p class="categoria">{estrellas}</p>
                    <p>{hotel['direccion']}, {hotel['ciudad']}</p>
                    <p class="precio">Precio por noche: {hotel['precio_noche']:.2f}€</p>
                    <p>Precio total estancia: {hotel['precio_total']:.2f}€</p>
                </div>
            '''
        
        html += '''
                <a href="/test" class="back-btn">Nueva búsqueda</a>
            </body>
        </html>
        '''
        
        return html


if __name__ == '__main__':
    try:
        # Iniciar behavior
        ab1 = Process(target=agentbehavior1, args=(cola1,))
        ab1.start()

        # Iniciar servidor
        app.run(host=hostname, port=port, debug=False)
        ab1.join()
        
    except Exception as e:
        if 'ab1' in locals():
            ab1.terminate()
        print('Error en el Agente de Alojamientos')