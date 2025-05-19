# -*- coding: utf-8 -*-
"""
*** Agente de Valoraciones (con RDF/OWL) ***

Este agente:
1. Valora planes de usuarios, instanciando valoraciones en RDF.
2. Genera recomendaciones proactivas basadas en valoraciones RDF.

@author: Arnau
"""

import argparse
import datetime
import random
import socket
import threading
import time
import uuid
import logging

from flask import Flask, request, Response, render_template
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from AgentUtil.Agent import Agent
from AgentUtil.ACLMessages import build_message, get_message_properties
from AgentUtil.ACL import ACL
from AgentUtil.FlaskServer import shutdown_server
from AgentUtil.DSO import DSO

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Argumentos
parser = argparse.ArgumentParser()
parser.add_argument('--open', action='store_true')
parser.add_argument('--port', type=int)
parser.add_argument('--dhost')
parser.add_argument('--dport', type=int)
args = parser.parse_args()

# Configuración de red
port = args.port if args.port else 9003
hostname = '0.0.0.0' if args.open else socket.gethostname()
dhostname = args.dhost if args.dhost else socket.gethostname()
dport = args.dport if args.dport else 9000

# Namespaces
agn = Namespace("http://www.agentes.org#")
onto = Namespace("http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2/")  # Actualizado al correcto

# Datos del agente
AgenteValoraciones = Agent('AgenteValoraciones',
    agn.AgenteValoraciones,
    f'http://{hostname}:{port}/comm',
    f'http://{hostname}:{port}/Stop')

DirectoryAgent = Agent('DirectoryAgent',
    agn.Directory,
    f'http://{dhostname}:{dport}/Register',
    f'http://{dhostname}:{dport}/Stop')

# RDF Store
g_store = Graph()
try:
    g_store.parse("entrega2.ttl", format="turtle")
except Exception as e:
    logger.warning(f"Ontología no cargada: {e}")

# Añade después de cargar la ontología:
print("Usuarios en el grafo:")
for s in g_store.subjects(RDF.type, URIRef("http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2/Usuario")):
    print(f"- {s}")

# Flask
app = Flask(__name__)
mss_cnt = 0

@app.route("/comm")
def comunicacion():
    global mss_cnt

    message = request.args['content']
    g_msg = Graph()
    g_msg.parse(data=message, format='xml')

    props = get_message_properties(g_msg)
    if props['performative'] != ACL.request:
        return Response(status=400)

    content = props['content']
    tipo = g_msg.value(subject=content, predicate=RDF.type)

    if tipo == onto.PeticionValoracion:
        usuario = g_msg.value(subject=content, predicate=onto.realizadaPorUsuario)
        plan = g_msg.value(subject=content, predicate=onto.sobrePlan)
        return procesar_valoracion(usuario, plan, props['sender'])

    # Buscar petición de recomendación
    for s, p, o in g_msg.triples((None, RDF.type, onto.PeticionRecomendacion)):
        usuario = g_msg.value(subject=s, predicate=onto.realizadaPorUsuario)
        if usuario:
            return Response(procesar_peticion_recomendacion(str(usuario), props['sender']), mimetype='text/xml')


    return Response(status=400)

@app.route("/Stop")
def stop():
    shutdown_server()
    return "Agente detenido"

@app.route("/admin", methods=['GET', 'POST'])
def admin():
    recomendacion = None

    if request.method == 'POST':
        usuario = request.form['usuario']

        # Obtener recomendación RDF
        rdf_msg = procesar_peticion_recomendacion(usuario, AgenteValoraciones.uri)

        # Parsear RDF para extraer el destino recomendado
        g = Graph()
        g.parse(data=rdf_msg, format='xml')
        for s in g.subjects(RDF.type, onto.RespuestaRecomendacion):
            destino = g.value(subject=s, predicate=onto.destinoRecomendado)
            if destino:
                recomendacion = str(destino)

    planes = [(s, g_store.value(s, onto.destino)) for s in g_store.subjects(RDF.type, onto.Plan)]
    valoraciones = [(s, g_store.value(s, onto.puntuacion)) for s in g_store.subjects(RDF.type, onto.Valoracion)]

    return render_template("valoraciones_admin.html", planes=planes, valoraciones=valoraciones, recomendacion=recomendacion)


def procesar_valoracion(usuario, plan, receiver):
    global mss_cnt

    id_val = URIRef(f"http://www.semanticweb.org/ontologia/valoracion/{uuid.uuid4()}")
    puntuacion = random.randint(1, 5)
    comentarios = ["Excelente", "Buena", "Aceptable", "Mejorable", "Mala"]
    comentario = comentarios[5 - puntuacion]

    g_store.add((id_val, RDF.type, onto.Valoracion))
    g_store.add((id_val, onto.deUsuario, usuario))
    g_store.add((id_val, onto.sobrePlan, plan))
    g_store.add((id_val, onto.puntuacion, Literal(puntuacion, datatype=XSD.integer)))
    g_store.add((id_val, RDFS.comment, Literal(comentario)))
    g_store.add((id_val, onto.fechaValoracion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))

    g_res = Graph()
    g_res.bind("onto", onto)
    g_res.add((id_val, RDF.type, onto.RespuestaValoracion))
    g_res += g_store.triples((id_val, None, None))

    mss_cnt += 1
    return Response(build_message(g_res, ACL.inform, AgenteValoraciones.uri, receiver, mss_cnt).serialize(format='xml'), mimetype='text/xml')

def procesar_peticion_recomendacion(usuario_uri, receptor_uri):
    global mss_cnt, g_store  # Cambiar dsgraph por g_store

    # Buscar destinos valorados por el usuario
    valorados = set()
    for val in g_store.subjects(RDF.type, onto.Valoracion):
        if (val, onto.deUsuario, Literal(usuario_uri)) in g_store:
            destino = g_store.value(subject=val, predicate=onto.sobrePlan)
            if destino:
                valorados.add(str(destino))

    # Buscar todos los destinos definidos en la ontología
    destinos_definidos = set()
    for dest in g_store.subjects(RDF.type, onto.Destino):
        destinos_definidos.add(str(dest))

    # Filtrar no visitados
    destinos_no_visitados = list(destinos_definidos - valorados)

    if destinos_no_visitados:
        destino_recomendado = random.choice(destinos_no_visitados)
    else:
        destino_recomendado = random.choice(list(destinos_definidos)) if destinos_definidos else "DestinoDesconocido"

    # Crear respuesta RDF
    g = Graph()
    g.bind("onto", onto)
    g.bind("rdf", RDF)
    g.bind("rdfs", RDFS)

    recomendacion_uri = URIRef(f"http://www.agentes.org/recomendacion/{uuid.uuid4()}")
    g.add((recomendacion_uri, RDF.type, onto.RespuestaRecomendacion))
    g.add((recomendacion_uri, onto.paraUsuario, Literal(usuario_uri)))
    g.add((recomendacion_uri, onto.destinoRecomendado, Literal(destino_recomendado)))
    g.add((recomendacion_uri, onto.fechaRecomendacion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
    g.add((recomendacion_uri, RDFS.comment, Literal("Recomendación generada automáticamente")))

    mss_cnt += 1
    return build_message(g, ACL.inform, sender=AgenteValoraciones.uri, receiver=receptor_uri, msgcnt=mss_cnt).serialize(format='xml')

def hilo_recomendaciones():
    while True:
        usuarios = set(g_store.objects(predicate=onto.deUsuario))
        for usuario in usuarios:
            if random.random() < 0.3:
                logger.info(f"Enviando recomendación proactiva a {usuario}")
                procesar_peticion_recomendacion(str(usuario), str(usuario))  # Corregido nombre de función
        time.sleep(60)

def hilo_valoraciones():
    while True:
        for plan in g_store.subjects(RDF.type, onto.Plan):
            estado = g_store.value(plan, onto.estado)
            if estado and str(estado) == 'finalizado':
                valorado = g_store.value(plan, onto.valoracionGenerada)
                if not valorado:
                    usuario = g_store.value(plan, onto.usuario)
                    procesar_valoracion(usuario, plan, usuario)
                    g_store.add((plan, onto.valoracionGenerada, Literal(True)))
        time.sleep(60)

if __name__ == '__main__':
    threading.Thread(target=hilo_recomendaciones, daemon=True).start()
    threading.Thread(target=hilo_valoraciones, daemon=True).start()
    app.run(host=hostname, port=port)
