# -*- coding: utf-8 -*-
"""
*** SimpleDirectoryAgent ***

Antes de ejecutar hay que añadir la raiz del proyecto a la variable PYTHONPATH

Agente que lleva un registro de otros agentes

Utiliza un registro simple que guarda en un grafo RDF

El registro no es persistente y se mantiene mientras el agente funciona

Las acciones que se pueden usar estan definidas en la ontología
directory-service-ontology.owl


@author: javier
"""

from multiprocessing import Process, Queue
import argparse
import logging

from flask import Flask, request, render_template
from rdflib import Graph, RDF, Namespace, RDFS, Literal
from rdflib.namespace import FOAF

from AgentUtil.ACL import ACL
from AgentUtil.FlaskServer import shutdown_server
from AgentUtil.Agent import Agent
from AgentUtil.ACLMessages import build_message, get_message_properties
from AgentUtil.Logging import config_logger
from AgentUtil.DSO import DSO
from AgentUtil.Util import gethostname
import socket

__author__ = 'javier'

# Definimos los parametros de la linea de comandos
parser = argparse.ArgumentParser()
parser.add_argument('--open', help="Define si el servidor est abierto al exterior o no", action='store_true',
                    default=False)
parser.add_argument('--verbose', help="Genera un log de la comunicacion del servidor web", action='store_true',
                        default=False)
parser.add_argument('--port', type=int, help="Puerto de comunicacion del agente")

# Logging
logger = config_logger(level=1)

# parsing de los parametros de la linea de comandos
args = parser.parse_args()

# Configuration stuff
if args.port is None:
    port = 9000
else:
    port = args.port

if args.open:
    hostname = '0.0.0.0'
    hostaddr = gethostname()
else:
    hostaddr = hostname = socket.gethostname()

print('DS Hostname =', hostaddr)

# Directory Service Graph
dsgraph = Graph()

# Vinculamos todos los espacios de nombre a utilizar
dsgraph.bind('acl', ACL)
dsgraph.bind('rdf', RDF)
dsgraph.bind('rdfs', RDFS)
dsgraph.bind('foaf', FOAF)
dsgraph.bind('dso', DSO)

agn = Namespace("http://www.agentes.org#")
DirectoryAgent = Agent('DirectoryAgent',
                       agn.Directory,
                       'http://%s:%d/Register' % (hostaddr, port),
                       'http://%s:%d/Stop' % (hostaddr, port))
app = Flask(__name__)

if not args.verbose:
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

mss_cnt = 0

cola1 = Queue()  # Cola de comunicacion entre procesos


@app.route("/Register")
def register():
    """
    Entry point del agente que recibe los mensajes de registro
    La respuesta es enviada al retornar la funcion,
    no hay necesidad de enviar el mensaje explicitamente

    Asumimos una version simplificada del protocolo FIPA-request
    en la que no enviamos el mesaje Agree cuando vamos a responder

    :return:
    """

    def process_register():
        """
        Procesa la solicitud de registro de un agente y ajusta las direcciones
        para garantizar que funcionen en entornos distribuidos
        """
        logger.info('Peticion de registro')

        # Extraer datos básicos del agente
        agn_add = gm.value(subject=content, predicate=DSO.Address)
        agn_name = gm.value(subject=content, predicate=FOAF.name)
        agn_uri = gm.value(subject=content, predicate=DSO.Uri)
        agn_type = gm.value(subject=content, predicate=DSO.AgentType)

        # Obtener la dirección IP real del cliente que se está registrando
        client_ip = request.remote_addr
        logger.info(f"IP del cliente que se registra: {client_ip}")

        # Verificar si la dirección del agente usa localhost o nombre de host
        agn_add_str = str(agn_add)
        if "localhost" in agn_add_str or "127.0.0.1" in agn_add_str or "DESKTOP-" in agn_add_str.upper():
            # Extraer puerto de la dirección original
            import re
            port_match = re.search(r":(\d+)/", agn_add_str)
            if port_match:
                port = port_match.group(1)
                # Construir nueva dirección con IP real
                new_address = f"http://{client_ip}:{port}/comm"
                logger.info(f"Reemplazando dirección {agn_add_str} por {new_address}")
                agn_add = Literal(new_address)

        # Añadimos la información en el grafo de registro
        dsgraph.add((agn_uri, RDF.type, FOAF.Agent))
        dsgraph.add((agn_uri, FOAF.name, agn_name))
        dsgraph.add((agn_uri, DSO.Address, agn_add))
        dsgraph.add((agn_uri, DSO.AgentType, agn_type))

        # Generamos un mensaje de respuesta
        return build_message(Graph(),
                             ACL.confirm,
                             sender=DirectoryAgent.uri,
                             receiver=agn_uri,
                             msgcnt=mss_cnt)

    def process_search():
        """
        Procesa una solicitud de búsqueda de agentes por tipo
        Devuelve todos los agentes encontrados, no solo el primero
        """
        logger.info('Petición de búsqueda')

        agn_type = gm.value(subject=content, predicate=DSO.AgentType)
        # Buscamos todas las coincidencias
        agents = list(dsgraph.subjects(DSO.AgentType, agn_type))
        
        if agents:
            gr = Graph()
            gr.bind('dso', DSO)
            rsp_obj = agn['Directory-response']
            
            # Añadir todos los agentes encontrados a la respuesta
            for agent_uri in agents:
                agent_add = dsgraph.value(subject=agent_uri, predicate=DSO.Address)
                # Crear un nodo único para cada resultado
                result_node = URIRef(f"{str(agent_uri)}-result")
                gr.add((rsp_obj, DSO.hasResult, result_node))
                gr.add((result_node, DSO.Address, agent_add))
                gr.add((result_node, DSO.Uri, agent_uri))
                
            return build_message(gr,
                           ACL.inform,
                           sender=DirectoryAgent.uri,
                           msgcnt=mss_cnt,
                           content=rsp_obj)
        else:
            # Si no encontramos nada retornamos un inform sin contenido
            return build_message(Graph(),
                           ACL.inform,
                           sender=DirectoryAgent.uri,
                           msgcnt=mss_cnt)

    global dsgraph
    global mss_cnt
    # Extraemos el mensaje y creamos un grafo con él
    message = request.args['content']
    gm = Graph()
    gm.parse(data=message, format='xml')

    msgdic = get_message_properties(gm)

    # Comprobamos que sea un mensaje FIPA ACL
    if not msgdic:
        # Si no es, respondemos que no hemos entendido el mensaje
        gr = build_message(Graph(),
                           ACL['not-understood'],
                           sender=DirectoryAgent.uri,
                           msgcnt=mss_cnt)
    else:
        # Obtenemos la performativa
        if msgdic['performative'] != ACL.request:
            # Si no es un request, respondemos que no hemos entendido el mensaje
            gr = build_message(Graph(),
                               ACL['not-understood'],
                               sender=DirectoryAgent.uri,
                               msgcnt=mss_cnt)
        else:
            # Extraemos el objeto del contenido que ha de ser una accion de la ontologia
            # de registro
            content = msgdic['content']
            # Averiguamos el tipo de la accion
            accion = gm.value(subject=content, predicate=RDF.type)

            # Accion de registro
            if accion == DSO.Register:
                gr = process_register()
            # Accion de busqueda
            elif accion == DSO.Search:
                gr = process_search()
            # No habia ninguna accion en el mensaje
            else:
                gr = build_message(Graph(),
                                   ACL['not-understood'],
                                   sender=DirectoryAgent.uri,
                                   msgcnt=mss_cnt)
    mss_cnt += 1
    return gr.serialize(format='xml')


@app.route('/info')
def info():
    """
    Entrada que da informacion sobre el agente a traves de una pagina web
    """
    global dsgraph
    global mss_cnt

    return render_template('info.html', nmess=mss_cnt, graph=dsgraph.serialize(format='turtle'))


@app.route("/stop")
def stop():
    """
    Entrada que para el agente
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


def agentbehavior1(cola):
    """
    Behaviour que simplemente espera mensajes de una cola y los imprime
    hasta que llega un 0 a la cola
    """
    fin = False
    while not fin:
        while cola.empty():
            pass
        v = cola.get()
        if v == 0:
            print(v)
            return 0
        else:
            print(v)


if __name__ == '__main__':
    # Ponemos en marcha los behaviours como procesos
    ab1 = Process(target=agentbehavior1, args=(cola1,))
    ab1.start()

    # Ponemos en marcha el servidor Flask
    app.run(host=hostname, port=port, debug=True)

    ab1.join()
    logger.info('The End')
