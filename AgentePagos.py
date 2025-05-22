# -*- coding: utf-8 -*-
"""
*** Agente de Pagos ***

Este agente procesa pagos, verifica facturas contra planes y valida que
los importes correspondan a los servicios contratados.

@author: Your Name
"""

from multiprocessing import Process, Queue
import socket
import argparse
import datetime
import uuid
import logging
import random
import time

from rdflib import Namespace, Graph, Literal, URIRef
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

# Configuration parser
parser = argparse.ArgumentParser()
parser.add_argument('--open', help="Define si el servidor esta abierto al exterior o no", action='store_true',
                    default=False)
parser.add_argument('--port', type=int, help="Puerto de comunicacion del agente")
parser.add_argument('--dhost', help="Host del agente de directorio")
parser.add_argument('--dport', type=int, help="Puerto del agente de directorio")

# parsing the args
args = parser.parse_args()

# Configuration stuff
if args.port is None:
    port = 9005
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

# Namespaces
agn = Namespace("http://www.agentes.org#")
onto = Namespace("http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2/")

# Contador de mensajes
mss_cnt = 0

# Datos del Agente
AgentePagos = Agent('AgentePagos',
                    agn.AgentePagos,
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

# Base de datos de pagos
pagos_db = Graph()
pagos_db.bind('onto', onto)
pagos_db.bind('xsd', XSD)

cola1 = Queue()
app = Flask(__name__)


@app.route("/comm")
def comunicacion():
    """
    Punto de entrada de comunicación para recibir peticiones de pagos
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
        
        # Petición de validación de pago
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionPago)):
            plan_uri = None
            factura_uri = None
            
            # Extraer el plan a pagar
            for s1, p1, o1 in gm.triples((s, onto.paraPlan, None)):
                plan_uri = o1
            
            # Extraer la factura
            for s1, p1, o1 in gm.triples((s, onto.conFactura, None)):
                factura_uri = o1
            
            if plan_uri and factura_uri:
                logger.info(f"Procesando validación de pago para plan: {plan_uri}")
                respuesta = procesar_validacion_pago(plan_uri, factura_uri, content)
                return Response(respuesta, mimetype='text/xml')
            else:
                logger.warning("Petición incompleta: falta plan o factura")
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
    return "Parando Servidor"


def tidyup():
    """
    Acciones previas a parar el agente
    """
    global cola1
    cola1.put(0)


def procesar_validacion_pago(plan_uri, factura_uri, content_uri):
    """
    Valida que la factura corresponda con el precio del plan
    
    :param plan_uri: URI del plan
    :param factura_uri: URI de la factura
    :param content_uri: URI del contenido de la petición original
    :return: Mensaje XML con la respuesta
    """
    global mss_cnt
    global pagos_db
    
    # Obtener el precio del plan
    plan_precio = None
    plan_id = None
    
    # Buscar en la base de datos local primero
    for s, p, o in pagos_db.triples((URIRef(plan_uri), onto.PrecioTotal, None)):
        plan_precio = float(o)
        plan_id = s
        break
    
    # Si no está en la base de datos local, buscar en el directorio
    if not plan_precio:
        try:
            # Aquí podríamos usar algún mecanismo para buscar el plan en otros agentes
            # Por ejemplo, consultando al AgentePlanes
            
            # Por ahora, buscamos en la ontología local
            for s, p, o in dsgraph.triples((URIRef(plan_uri), onto.PrecioTotal, None)):
                plan_precio = float(o)
                plan_id = s
                break
        except Exception as e:
            logger.error(f"Error al buscar precio del plan: {e}")
    
    # Obtener el importe de la factura
    factura_importe = None
    factura_id = None
    
    for s, p, o in dsgraph.triples((URIRef(factura_uri), onto.Importe, None)):
        factura_importe = float(o)
        factura_id = s
        break
    
    # Si no encontramos la factura en la ontología, buscar en el grafo entrante
    # (Esto asume que la petición también contiene los detalles de la factura)
    if not factura_importe:
        for s, p, o in gm.triples((URIRef(factura_uri), onto.Importe, None)):
            factura_importe = float(o)
            factura_id = s
            break
    
    # Crear grafo para la respuesta
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    
    respuesta_id = URIRef(f'respuesta_pago_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaPagoRecibido))
    g.add((respuesta_id, onto.respuestaA, URIRef(content_uri)))
    
    # Validar que la factura coincida con el precio del plan
    if plan_precio is not None and factura_importe is not None:
        # Verificar si los precios coinciden (con un pequeño margen de error)
        if abs(plan_precio - factura_importe) < 0.01:
            # Pago válido
            g.add((respuesta_id, onto.estadoPago, Literal("Validado")))
            g.add((respuesta_id, RDFS.comment, Literal("El pago ha sido validado correctamente")))
            
            # Registrar el pago como realizado
            pago_id = URIRef(f'pago_{str(uuid.uuid4())}')
            pagos_db.add((pago_id, RDF.type, onto.Pago))
            pagos_db.add((pago_id, onto.paraPlan, URIRef(plan_uri)))
            pagos_db.add((pago_id, onto.estado, Literal("Completado")))
            pagos_db.add((pago_id, onto.fechaPago, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
            pagos_db.add((pago_id, onto.importe, Literal(factura_importe, datatype=XSD.float)))
            
            # Actualizar estado del plan
            if plan_id:
                pagos_db.add((URIRef(plan_id), onto.estado, Literal("pagado")))
                
            logger.info(f"Pago validado para el plan {plan_uri} - Importe: {factura_importe}")
        else:
            # Pago inválido - importes no coinciden
            g.add((respuesta_id, onto.estadoPago, Literal("Rechazado")))
            g.add((respuesta_id, RDFS.comment, 
                  Literal(f"El importe no coincide (Plan: {plan_precio}, Factura: {factura_importe})")))
            logger.warning(f"Pago rechazado - Importes no coinciden: Plan={plan_precio}, Factura={factura_importe}")
    else:
        # No se pudo validar el pago
        g.add((respuesta_id, onto.estadoPago, Literal("Error")))
        g.add((respuesta_id, RDFS.comment, 
               Literal(f"No se pudo validar el pago (Plan encontrado: {plan_precio is not None}, Factura encontrada: {factura_importe is not None})")))
        logger.error(f"No se pudo validar el pago - Plan/Factura no encontrado")
    
    # Construir mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform, 
                         sender=AgentePagos.uri, 
                         receiver=content_uri, 
                         msgcnt=mss_cnt).serialize(format='xml')


def verificar_pagos_pendientes():
    """
    Verifica y procesa pagos pendientes en la base de datos
    """
    global pagos_db
    
    logger.info("Verificando pagos pendientes")
    
    # Buscar planes listos sin pago procesado
    planes_listos = []
    
    for s, p, o in pagos_db.triples((None, onto.estado, Literal("listo"))):
        # Verificar si ya tiene un pago asociado
        tiene_pago = False
        for s1, p1, o1 in pagos_db.triples((None, onto.paraPlan, s)):
            tiene_pago = True
            break
        
        if not tiene_pago:
            planes_listos.append(s)
    
    # Procesar cada plan listo
    for plan_uri in planes_listos:
        logger.info(f"Procesando pago pendiente para plan: {plan_uri}")
        
        # Obtener precio del plan
        plan_precio = None
        for s, p, o in pagos_db.triples((plan_uri, onto.PrecioTotal, None)):
            plan_precio = float(o)
            break
        
        if plan_precio:
            # Crear un pago automático
            pago_id = URIRef(f'pago_auto_{str(uuid.uuid4())}')
            pagos_db.add((pago_id, RDF.type, onto.Pago))
            pagos_db.add((pago_id, onto.paraPlan, plan_uri))
            pagos_db.add((pago_id, onto.estado, Literal("Pendiente")))
            pagos_db.add((pago_id, onto.fechaCreacion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
            pagos_db.add((pago_id, onto.importe, Literal(plan_precio, datatype=XSD.float)))
            
            logger.info(f"Pago pendiente creado para el plan {plan_uri} - Importe: {plan_precio}")


def agentbehavior1(cola):
    """
    Comportamiento del agente - Registrarse en el directorio
    """
    global mss_cnt
    # Registrar el agente en el servicio de directorio
    gmess = Graph()
    gmess.bind('foaf', FOAF)
    gmess.bind('dso', DSO)
    reg_obj = agn[AgentePagos.name + '-Register']
    gmess.add((reg_obj, RDF.type, DSO.Register))
    gmess.add((reg_obj, DSO.Uri, AgentePagos.uri))
    gmess.add((reg_obj, FOAF.name, Literal(AgentePagos.name)))
    gmess.add((reg_obj, DSO.Address, Literal(AgentePagos.address)))
    gmess.add((reg_obj, DSO.AgentType, DSO.PaymentAgent))

    # Lo metemos en el registro de servicios
    try:
        send_message(
            build_message(gmess, ACL.request,
                        sender=AgentePagos.uri,
                        receiver=DirectoryAgent.uri,
                        content=reg_obj,
                        msgcnt=mss_cnt),
            DirectoryAgent.address
        )
        mss_cnt += 1
        logger.info("Registro en el directorio completado")
    except Exception as e:
        logger.error(f"Error al registrarse en el directorio: {e}")

    # Bucle de comportamiento
    while True:
        try:
            # Verificar pagos pendientes cada 10 segundos
            verificar_pagos_pendientes()
            time.sleep(10)
        except Exception as e:
            logger.error(f"Error en el comportamiento del agente: {e}")
            time.sleep(5)


@app.route("/test", methods=['GET', 'POST'])
def test_interface():
    """
    Interfaz web para probar el agente de pagos
    """
    if request.method == 'GET':
        # Mostrar un formulario para pruebas
        return '''
        <html>
            <head>
                <title>Test Agente Pagos</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; }
                    .form-group { margin-bottom: 15px; }
                    label { display: block; margin-bottom: 5px; }
                    input, select { padding: 8px; width: 300px; }
                    button { padding: 10px 15px; background-color: #4CAF50; color: white; border: none; cursor: pointer; }
                    h2 { margin-top: 30px; }
                    table { border-collapse: collapse; width: 100%; }
                    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
                    tr:nth-child(even) { background-color: #f2f2f2; }
                    .status { font-weight: bold; }
                    .valid { color: green; }
                    .invalid { color: red; }
                </style>
            </head>
            <body>
                <h1>Test Agente Pagos</h1>
                
                <form method="post">
                    <div class="form-group">
                        <label>ID del Plan:</label>
                        <input type="text" name="plan_id" required placeholder="URI del plan a validar">
                    </div>
                    
                    <div class="form-group">
                        <label>Importe de la Factura:</label>
                        <input type="number" name="factura_importe" step="0.01" required>
                    </div>
                    
                    <button type="submit">Validar Pago</button>
                </form>
                
                <h2>Pagos Registrados</h2>
                <table>
                    <tr>
                        <th>Plan ID</th>
                        <th>Importe</th>
                        <th>Estado</th>
                        <th>Fecha</th>
                    </tr>
                    '''+ ''.join([f'''
                    <tr>
                        <td>{str(plan)}</td>
                        <td>{str(pagos_db.value(subject=pago, predicate=onto.importe))}</td>
                        <td class="status {'valid' if str(pagos_db.value(subject=pago, predicate=onto.estado)) == 'Completado' else ''}">{str(pagos_db.value(subject=pago, predicate=onto.estado))}</td>
                        <td>{str(pagos_db.value(subject=pago, predicate=onto.fechaPago) or pagos_db.value(subject=pago, predicate=onto.fechaCreacion))}</td>
                    </tr>
                    '''
                    for pago, plan in [(s, pagos_db.value(subject=s, predicate=onto.paraPlan)) 
                                      for s in pagos_db.subjects(RDF.type, onto.Pago)]]) + '''
                </table>
            </body>
        </html>
        '''
    else:
        # Procesar el formulario POST
        plan_id = request.form['plan_id']
        factura_importe = float(request.form['factura_importe'])
        
        # Crear una factura ficticia
        factura_id = f"factura_{str(uuid.uuid4())}"
        
        # Simular una petición de validación
        g = Graph()
        g.bind('rdf', RDF)
        g.bind('onto', onto)
        
        peticion_id = URIRef(f'peticion_test_{str(uuid.uuid4())}')
        g.add((peticion_id, RDF.type, onto.PeticionPago))
        
        plan_uri = URIRef(plan_id)
        g.add((peticion_id, onto.paraPlan, plan_uri))
        
        factura_uri = URIRef(factura_id)
        g.add((peticion_id, onto.conFactura, factura_uri))
        g.add((factura_uri, RDF.type, onto.Factura))
        g.add((factura_uri, onto.Importe, Literal(factura_importe, datatype=XSD.float)))
        
        # Procesar la validación
        respuesta = procesar_validacion_pago(plan_id, factura_id, AgentePagos.uri)
        
        # Parsear la respuesta para mostrar el resultado
        gr = Graph()
        gr.parse(data=respuesta, format='xml')
        
        estado = None
        comentario = None
        for s, p, o in gr.triples((None, onto.estadoPago, None)):
            estado = str(o)
        
        for s, p, o in gr.triples((None, RDFS.comment, None)):
            comentario = str(o)
        
        return f'''
        <html>
            <head>
                <title>Resultado de Validación</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .result {{ margin: 20px 0; padding: 15px; border-radius: 5px; }}
                    .valid {{ background-color: #d4edda; color: #155724; }}
                    .invalid {{ background-color: #f8d7da; color: #721c24; }}
                    .error {{ background-color: #fff3cd; color: #856404; }}
                    .back-btn {{ margin-top: 20px; padding: 10px; background: #4CAF50; color: white; text-decoration: none; display: inline-block; border-radius: 5px; }}
                </style>
            </head>
            <body>
                <h1>Resultado de la Validación de Pago</h1>
                
                <div class="result {'valid' if estado == 'Validado' else 'invalid' if estado == 'Rechazado' else 'error'}">
                    <h2>Estado: {estado}</h2>
                    <p>{comentario}</p>
                </div>
                
                <p>Plan ID: {plan_id}</p>
                <p>Importe de factura: {factura_importe}€</p>
                
                <a href="/test" class="back-btn">Volver</a>
            </body>
        </html>
        '''


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
        logger.info('Agente de Pagos finalizado')
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals():
            ab1.terminate()
        print('Error en el Agente de Pagos')