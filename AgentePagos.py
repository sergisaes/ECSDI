# -*- coding: utf-8 -*-
"""
*** Agente de Pagos ***

Este agente procesa pagos, verifica facturas contra planes y valida que
los importes correspondan a los servicios contratados.

@author: Arnau
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
        
        # Procesar petición genérica de pago
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionPago)):
            plan_uri = None
            cuenta_bancaria = None
            importe = None
            tipo_peticion = "Genérica"
            
            # Extraer el plan a pagar
            for s1, p1, o1 in gm.triples((s, onto.tieneComoPlan, None)):
                plan_uri = o1
                
            # Extraer importe
            for s1, p1, o1 in gm.triples((s, onto.ImportePago, None)):
                importe = float(o1)
            
            # Detectar tipo específico de petición de pago
            for s1, p1, o1 in gm.triples((s, RDF.type, onto.PeticionPagoPorContrato)):
                tipo_peticion = "Contrato"
                
                # Extraer cuenta bancaria para pagos por contrato
                for s2, p2, o2 in gm.triples((s, onto.CuentaBancaria, None)):
                    cuenta_bancaria = str(o2)
                break
                
            for s1, p1, o1 in gm.triples((s, RDF.type, onto.PeticionPagoPorPasarela)):
                tipo_peticion = "Pasarela"
                break
            
            logger.info(f"Recibida petición de pago por {tipo_peticion} para plan: {plan_uri}")
            
            # Si tenemos al menos la información mínima necesaria
            if plan_uri:
                if tipo_peticion == "Contrato":
                    respuesta = procesar_pago_contrato(plan_uri, importe, cuenta_bancaria, msgdic['sender'])
                else:
                    # Pasarela
                    respuesta = procesar_pago_pasarela(plan_uri, importe, msgdic['sender'])
                    
                return Response(respuesta, mimetype='text/xml')
            else:
                logger.warning("Petición incompleta: falta el plan")
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
        # Verificar si los precios coinciden
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
    Verifica y carga planes pendientes en la base de datos (sin procesarlos)
    """
    try:
        # Intentar cargar el archivo de planes aceptados
        planes_graph = Graph()
        planes_graph.parse("planes_aceptados.ttl", format="turtle")
        logger.info("Archivo de planes aceptados cargado correctamente")
        
        # Buscar planes listos sin pago procesado
        planes_listos = []
        
        for s, p, o in planes_graph.triples((None, onto.estado, Literal("listo"))):
            # Verificar si ya está en nuestra base de datos
            tiene_pago = False
            for s1, p1, o1 in pagos_db.triples((None, onto.paraPlan, s)):
                tiene_pago = True
                break
            
            if not tiene_pago:
                # Solo importar a la base de datos para mostrar en la interfaz
                for s2, p2, o2 in planes_graph.triples((s, None, None)):
                    pagos_db.add((s2, p2, o2))
                    
                logger.info(f"Plan {s} importado para pago manual")
        
        # No procesar automáticamente
    except Exception as e:
        if "No such file or directory" in str(e):
            logger.info("No se encontró archivo de planes aceptados. Esperando...")
        else:
            logger.error(f"Error al procesar pagos pendientes: {e}")


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
        # Encontrar todos los planes pendientes
        planes_pendientes = []
        for s, p, o in pagos_db.triples((None, onto.estado, Literal("listo"))):
            precio = pagos_db.value(subject=s, predicate=onto.PrecioTotal)
            comentario = pagos_db.value(subject=s, predicate=RDFS.comment)
            planes_pendientes.append({
                'uri': str(s),
                'precio': float(precio) if precio else 0,
                'descripcion': str(comentario) if comentario else 'Sin descripción'
            })
        
        planes_html = ''.join([f'''
        <tr>
            <td>{p['uri']}</td>
            <td>{p['descripcion']}</td>
            <td>{p['precio']:.2f}€</td>
            <td>
                <form method="post" action="/test">
                    <input type="hidden" name="plan_id" value="{p['uri']}">
                    <input type="hidden" name="importe" value="{p['precio']}">
                    <select name="payment_method" style="margin-right: 10px;">
                        <option value="pasarela">Pasarela</option>
                        <option value="contrato">Contrato</option>
                    </select>
                    <button type="submit">Pagar</button>
                </form>
            </td>
        </tr>
        ''' for p in planes_pendientes])
        
        # Añadir una sección de planes pendientes antes de la tabla de pagos registrados
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
                    .conditional { display: none; }
                </style>
                <script>
                    function togglePaymentMethod() {
                        var method = document.getElementById('payment_method').value;
                        var bankAccount = document.getElementById('bank_account_group');
                        
                        if (method === 'contrato') {
                            bankAccount.style.display = 'block';
                        } else {
                            bankAccount.style.display = 'none';
                        }
                    }
                </script>
            </head>
            <body>
                <h1>Test Agente Pagos</h1>
                
                <form method="post">
                    <div class="form-group">
                        <label>ID del Plan:</label>
                        <input type="text" name="plan_id" required placeholder="URI del plan a validar">
                    </div>
                    
                    <div class="form-group">
                        <label>Importe a Pagar:</label>
                        <input type="number" name="importe" step="0.01" required>
                    </div>
                    
                    <div class="form-group">
                        <label>Método de Pago:</label>
                        <select name="payment_method" id="payment_method" onchange="togglePaymentMethod()">
                            <option value="pasarela">Pasarela de Pago</option>
                            <option value="contrato">Contrato Bancario</option>
                        </select>
                    </div>
                    
                    <div class="form-group conditional" id="bank_account_group">
                        <label>Número de Cuenta Bancaria:</label>
                        <input type="text" name="bank_account" placeholder="ES21 1234 5678 90 1234567890">
                    </div>
                    
                    <button type="submit">Procesar Pago</button>
                </form>
                
                <h2>Planes Pendientes de Pago</h2>
                <table>
                    <tr>
                        <th>Plan ID</th>
                        <th>Descripción</th>
                        <th>Importe</th>
                        <th>Acciones</th>
                    </tr>
                    ''' + planes_html + '''
                </table>
                
                <h2>Pagos Registrados</h2>
                <table>
                    <tr>
                        <th>Plan ID</th>
                        <th>Importe</th>
                        <th>Método</th>
                        <th>Estado</th>
                        <th>Fecha</th>
                    </tr>
                    '''+ ''.join([f'''
                    <tr>
                        <td>{str(plan)}</td>
                        <td>{str(pagos_db.value(subject=pago, predicate=onto.importe))}</td>
                        <td>{"Contrato" if pagos_db.value(subject=pago, predicate=onto.CuentaBancaria) else "Pasarela"}</td>
                        <td class="status {'valid' if str(pagos_db.value(subject=pago, predicate=onto.estado)) == 'Completado' else 'invalid'}">{str(pagos_db.value(subject=pago, predicate=onto.estado))}</td>
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
        importe = float(request.form['importe'])
        payment_method = request.form['payment_method']
        bank_account = request.form.get('bank_account', '')
        
        # Crear grafo para la petición
        g = Graph()
        g.bind('rdf', RDF)
        g.bind('onto', onto)
        g.bind('xsd', XSD)
        
        peticion_id = URIRef(f'peticion_test_{str(uuid.uuid4())}')
        
        # Tipo de petición según método de pago
        if payment_method == 'contrato':
            g.add((peticion_id, RDF.type, onto.PeticionPagoPorContrato))
            g.add((peticion_id, onto.CuentaBancaria, Literal(bank_account)))
        else:
            g.add((peticion_id, RDF.type, onto.PeticionPagoPorPasarela))
        
        # Datos comunes de la petición
        g.add((peticion_id, RDF.type, onto.PeticionPago))
        g.add((peticion_id, onto.tieneComoPlan, URIRef(plan_id)))
        g.add((peticion_id, onto.ImportePago, Literal(importe, datatype=XSD.float)))
        
        # Procesar el pago según el método
        if payment_method == 'contrato':
            respuesta = procesar_pago_contrato(plan_id, importe, bank_account, AgentePagos.uri)
        else:
            respuesta = procesar_pago_pasarela(plan_id, importe, AgentePagos.uri)
        
        # Parsear la respuesta para mostrar el resultado
        gr = Graph()
        gr.parse(data=respuesta, format='xml')
        
        # Obtener datos de la respuesta
        estado = None
        comentario = None
        for s, p, o in gr.triples((None, onto.estadoPago, None)):
            estado = str(o)
        
        for s, p, o in gr.triples((None, RDFS.comment, None)):
            comentario = str(o)
        
        # Determinar el tipo de respuesta
        tipo_respuesta = "Desconocido"
        for s, p, o in gr.triples((None, RDF.type, onto.RespuestaPagoContrato)):
            tipo_respuesta = "Contrato"
        
        for s, p, o in gr.triples((None, RDF.type, onto.RespuestaPagoRecibido)):
            tipo_respuesta = "Pasarela"
        
        return f'''
        <html>
            <head>
                <title>Resultado de Pago</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .result {{ margin: 20px 0; padding: 15px; border-radius: 5px; }}
                    .valid {{ background-color: #d4edda; color: #155724; }}
                    .invalid {{ background-color: #f8d7da; color: #721c24; }}
                    .error {{ background-color: #fff3cd; color: #856404; }}
                    table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
                    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                    th {{ background-color: #f2f2f2; }}
                    .back-btn {{ margin-top: 20px; padding: 10px; background: #4CAF50; color: white; text-decoration: none; display: inline-block; border-radius: 5px; }}
                </style>
            </head>
            <body>
                <h1>Resultado del Pago</h1>
                
                <div class="result {'valid' if estado == 'Validado' else 'invalid' if estado == 'Rechazado' else 'error'}">
                    <h2>Estado: {estado}</h2>
                    <p>{comentario}</p>
                </div>
                
                <table>
                    <tr>
                        <th>Tipo de pago</th>
                        <td>{tipo_respuesta}</td>
                    </tr>
                    <tr>
                        <th>Plan ID</th>
                        <td>{plan_id}</td>
                    </tr>
                    <tr>
                        <th>Importe</th>
                        <td>{importe}€</td>
                    </tr>
                    <tr>
                        <th>Método</th>
                        <td>{"Contrato Bancario" if payment_method == 'contrato' else "Pasarela de Pago"}</td>
                    </tr>
                    {'<tr><th>Cuenta Bancaria</th><td>' + bank_account + '</td></tr>' if payment_method == 'contrato' else ''}
                </table>
                
                <a href="/test" class="back-btn">Volver</a>
            </body>
        </html>
        '''


def procesar_peticion_pago_automatica(peticion_id, plan_uri, importe):
    """
    Procesa una petición de pago automática
    
    :param peticion_id: URI de la petición de pago
    :param plan_uri: URI del plan
    :param importe: Importe a pagar
    :return: True si el pago se ha realizado correctamente
    """
    global pagos_db
    
    try:
        # Crear pago y respuesta automáticos
        pago_id = URIRef(f'pago_auto_{str(uuid.uuid4())}')
        pagos_db.add((pago_id, RDF.type, onto.Pago))
        pagos_db.add((pago_id, onto.paraPlan, plan_uri))
        pagos_db.add((pago_id, onto.estado, Literal("Completado")))
        pagos_db.add((pago_id, onto.fechaCreacion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
        pagos_db.add((pago_id, onto.fechaPago, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
        pagos_db.add((pago_id, onto.importe, Literal(importe, datatype=XSD.float)))
        
        # Crear respuesta de pago recibido
        respuesta_id = URIRef(f'respuesta_pago_{str(uuid.uuid4())}')
        pagos_db.add((respuesta_id, RDF.type, onto.RespuestaPagoRecibido))
        pagos_db.add((respuesta_id, onto.IdPlan, Literal(str(plan_uri).split('_')[-1])))
        pagos_db.add((respuesta_id, onto.fechaCreacion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
        pagos_db.add((respuesta_id, onto.estadoPago, Literal("Validado")))
        
        # Actualizar estado del plan
        pagos_db.remove((plan_uri, onto.estado, None))
        pagos_db.add((plan_uri, onto.estado, Literal("pagado")))
        
        # También actualizar el archivo original
        planes_graph = Graph()
        planes_graph.parse("planes_aceptados.ttl", format="turtle")
        planes_graph.remove((plan_uri, onto.estado, None))
        planes_graph.add((plan_uri, onto.estado, Literal("pagado")))
        
        with open("planes_aceptados.ttl", 'wb') as f:
            serialized_data = planes_graph.serialize(format='turtle')
            if isinstance(serialized_data, str):
                serialized_data = serialized_data.encode('utf-8')
            f.write(serialized_data)
        
        return True
    
    except Exception as e:
        logger.error(f"Error al procesar pago automático: {e}")
        return False


def procesar_pago_contrato(plan_uri, importe, cuenta_bancaria, sender_uri):
    """
    Procesa un pago por contrato bancario
    
    :param plan_uri: URI del plan a pagar
    :param importe: Importe a pagar
    :param cuenta_bancaria: Número de cuenta bancaria
    :param sender_uri: URI del remitente
    :return: Mensaje XML con la respuesta
    """
    global mss_cnt
    global pagos_db
    
    # Validar cuenta bancaria (simulado)
    cuenta_valida = len(str(cuenta_bancaria or "")) >= 10
    
    # Crear grafo para la respuesta
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    
    respuesta_id = URIRef(f'respuesta_contrato_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaPagoContrato))
    
    # Identificador del plan
    plan_id = str(plan_uri).split('_')[-1]
    g.add((respuesta_id, onto.IdPlan, Literal(plan_id)))
    
    # Importe del pago
    if importe:
        g.add((respuesta_id, onto.importe, Literal(importe, datatype=XSD.float)))
    
    if cuenta_valida:
        # Crear pago si la cuenta es válida
        pago_id = URIRef(f'pago_contrato_{str(uuid.uuid4())}')
        pagos_db.add((pago_id, RDF.type, onto.Pago))
        pagos_db.add((pago_id, onto.paraPlan, URIRef(plan_uri)))
        pagos_db.add((pago_id, onto.estado, Literal("Completado")))
        pagos_db.add((pago_id, onto.fechaCreacion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
        pagos_db.add((pago_id, onto.fechaPago, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
        pagos_db.add((pago_id, onto.importe, Literal(importe, datatype=XSD.float)))
        pagos_db.add((pago_id, onto.CuentaBancaria, Literal(str(cuenta_bancaria))))
        
        # Actualizar estado del plan
        for s, p, o in pagos_db.triples((URIRef(plan_uri), None, None)):
            if p == onto.estado:
                pagos_db.remove((s, p, o))
        pagos_db.add((URIRef(plan_uri), onto.estado, Literal("pagado")))
        
        # Actualizar el archivo original si existe
        try:
            planes_graph = Graph()
            planes_graph.parse("planes_aceptados.ttl", format="turtle")
            
            for s, p, o in planes_graph.triples((URIRef(plan_uri), onto.estado, None)):
                planes_graph.remove((s, p, o))
            
            planes_graph.add((URIRef(plan_uri), onto.estado, Literal("pagado")))
            
            with open("planes_aceptados.ttl", 'wb') as f:
                serialized_data = planes_graph.serialize(format='turtle')
                if isinstance(serialized_data, str):
                    serialized_data = serialized_data.encode('utf-8')
                f.write(serialized_data)
        except Exception as e:
            logger.warning(f"No se pudo actualizar planes_aceptados.ttl: {e}")
        
        g.add((respuesta_id, onto.estadoPago, Literal("Validado")))
        g.add((respuesta_id, RDFS.comment, Literal("Pago por contrato validado correctamente")))
    else:
        g.add((respuesta_id, onto.estadoPago, Literal("Rechazado")))
        g.add((respuesta_id, RDFS.comment, Literal("Cuenta bancaria inválida")))
    
    # Construir mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform, 
                         sender=AgentePagos.uri, 
                         receiver=sender_uri, 
                         msgcnt=mss_cnt).serialize(format='xml')

def procesar_pago_pasarela(plan_uri, importe, sender_uri):
    """
    Procesa un pago por pasarela de pago
    
    :param plan_uri: URI del plan a pagar
    :param importe: Importe a pagar
    :param sender_uri: URI del remitente
    :return: Mensaje XML con la respuesta
    """
    global mss_cnt
    global pagos_db
    
    # Crear grafo para la respuesta
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    
    respuesta_id = URIRef(f'respuesta_pasarela_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaPagoRecibido))
    
    # Identificador del plan
    plan_id = str(plan_uri).split('_')[-1]
    g.add((respuesta_id, onto.IdPlan, Literal(plan_id)))
    
    # Crear pago
    pago_id = URIRef(f'pago_pasarela_{str(uuid.uuid4())}')
    pagos_db.add((pago_id, RDF.type, onto.Pago))
    pagos_db.add((pago_id, onto.paraPlan, URIRef(plan_uri)))
    pagos_db.add((pago_id, onto.estado, Literal("Completado")))
    pagos_db.add((pago_id, onto.fechaCreacion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
    pagos_db.add((pago_id, onto.fechaPago, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
    pagos_db.add((pago_id, onto.importe, Literal(importe, datatype=XSD.float)))
    
    # Actualizar estado del plan
    for s, p, o in pagos_db.triples((URIRef(plan_uri), None, None)):
        if p == onto.estado:
            pagos_db.remove((s, p, o))
    pagos_db.add((URIRef(plan_uri), onto.estado, Literal("pagado")))
    
    # Actualizar el archivo original si existe
    try:
        planes_graph = Graph()
        planes_graph.parse("planes_aceptados.ttl", format="turtle")
        
        for s, p, o in planes_graph.triples((URIRef(plan_uri), onto.estado, None)):
            planes_graph.remove((s, p, o))
        
        planes_graph.add((URIRef(plan_uri), onto.estado, Literal("pagado")))
        
        with open("planes_aceptados.ttl", 'wb') as f:
            serialized_data = planes_graph.serialize(format='turtle')
            if isinstance(serialized_data, str):
                serialized_data = serialized_data.encode('utf-8')
            f.write(serialized_data)
    except Exception as e:
        logger.warning(f"No se pudo actualizar planes_aceptados.ttl: {e}")
    
    g.add((respuesta_id, onto.estadoPago, Literal("Validado")))
    g.add((respuesta_id, RDFS.comment, Literal("Pago por pasarela validado correctamente")))
    

    # Construir mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform, 
                         sender=AgentePagos.uri, 
                         receiver=sender_uri, 
                         msgcnt=mss_cnt).serialize(format='xml')


if __name__ == '__main__':
    try:
        # Iniciar el proceso de comportamiento del agente
        ab1 = Process(target=agentbehavior1, args=(cola1,))
        ab1.start()

        logger.info(f"Iniciando servidor en {hostname}:{port}")
        # Iniciar el servidor Flask
        app.run(host=hostname, port=port, debug=False)

        # Esperar a que termine el proceso de comportamiento
        ab1.join()
        logger.info('Agente de Pagos finalizado')
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals():
            ab1.terminate()
        print('Error en el Agente de Pagos')