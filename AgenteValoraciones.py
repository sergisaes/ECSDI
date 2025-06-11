# -*- coding: utf-8 -*-
"""
*** Agente de Valoraciones (con RDF/OWL) ***

Este agente:
1. Recibe notificaciones del AgenteMantenedor sobre planes que necesitan valoración
2. Solicita valoración al usuario mediante una interfaz web
3. Registra las valoraciones de los usuarios en RDF
4. Genera recomendaciones personalizadas basadas en perfiles colaborativos

Interacción con AgenteMantenedor:
- Recibe: NotificacionPlanTerminado (plan + usuario)
- Responde: ConfirmacionSolicitudValoracion
- Flujo: AgenteMantenedor → AgenteValoraciones → Usuario → Valoración → Perfiles → Recomendaciones

@author: Laura
"""

import argparse
import datetime
import random
import socket
import threading
import time
import uuid
import logging
import os
import datetime
import threading
import time
from datetime import date
from multiprocessing import Process, Queue

from flask import Flask, request, Response, render_template
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD
import requests
import traceback
from rdflib.namespace import FOAF

from AgentUtil.Agent import Agent
from AgentUtil.ACLMessages import build_message, get_message_properties
from AgentUtil.ACL import ACL
from AgentUtil.FlaskServer import shutdown_server
from AgentUtil.DSO import DSO
from AgentUtil.ACLMessages import send_message

# Configuración de tiempos para pruebas (en segundos)
TIEMPO_ENTRE_LECTURAS_PLANES = 300  # Leer planes cada 10 segundos 
TIEMPO_ENTRE_VALORACIONES = 15     # Solicitar valoraciones cada 15 segundos
TIEMPO_ENTRE_RECOMENDACIONES_ACTIVIDAD = 600  # Recomendar cada 10 minutos


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


# Sistema de perfiles colaborativos
class PerfilColaborativo:
    def __init__(self, nombre):
        self.nombre = nombre  # Identificador del perfil (ej. "Culturales", "Aventureros")
        self.preferencias = {
            'Cultural': 0,
            'Aventura': 0, 
            'Gastronomica': 0,
            'Naturaleza': 0,
            'Exterior': 0,
            'Interior': 0
        }
        self.ciudades_populares = {}  # {ciudad: frecuencia}
        self.actividades_valoradas = {}  # {tipo_actividad: [puntuaciones]}
        self.usuarios = set()  # Conjunto de usuarios asignados a este perfil
    
    def actualizar_preferencias(self, tipo_actividad, puntuacion):
        """Actualiza las preferencias colectivas del grupo"""
        if tipo_actividad in self.preferencias:
            # Ponderamos menos cada actualización individual para representar el consenso colectivo
            incremento = puntuacion / (10.0 * (1 + len(self.usuarios)/10))  # El efecto disminuye con más usuarios
            self.preferencias[tipo_actividad] += incremento
    
    def registrar_ciudad(self, ciudad):
        """Registra una ciudad visitada por algún usuario del grupo"""
        self.ciudades_populares[ciudad] = self.ciudades_populares.get(ciudad, 0) + 1
    
    def registrar_valoracion(self, tipo_actividad, puntuacion):
        """Registra una valoración para un tipo de actividad"""
        if tipo_actividad not in self.actividades_valoradas:
            self.actividades_valoradas[tipo_actividad] = []
        self.actividades_valoradas[tipo_actividad].append(puntuacion)
    
    def añadir_usuario(self, usuario_uri):
        """Añade un usuario a este perfil"""
        self.usuarios.add(usuario_uri)
    
    def calcular_prioridad_actividad(self):
        """Calcula qué tipo de actividad es la favorita para este grupo"""
        return max(self.preferencias.items(), key=lambda x: x[1])[0] if self.preferencias else None
    
    def calcular_ciudad_recomendada(self, ciudades_visitadas):
        """Calcula la ciudad más relevante para recomendar basada en las preferencias del grupo"""
        # Filtrar ciudades ya visitadas
        ciudades_no_visitadas = {c: f for c, f in self.ciudades_populares.items() if c not in ciudades_visitadas}
        if ciudades_no_visitadas:
            # Recomendar la ciudad más popular entre los miembros del grupo
            return max(ciudades_no_visitadas.items(), key=lambda x: x[1])[0]
        return None

# Gestión de perfiles colaborativos
perfiles_colaborativos = {
    'Culturales': PerfilColaborativo('Culturales'),  # Aficionados a museos, monumentos, etc.
    'Aventureros': PerfilColaborativo('Aventureros'), # Prefieren actividades de aventura y exterior
    'Gastronómicos': PerfilColaborativo('Gastronómicos'), # Interesados en gastronomía local
    'Naturalistas': PerfilColaborativo('Naturalistas')  # Prefieren entornos naturales
}

# Mapa de usuarios a su perfil asignado
usuarios_perfiles = {}  # {usuario_uri: perfil_nombre}

def obtener_perfil_usuario(usuario_uri):
    """
    Determina el perfil colaborativo al que pertenece un usuario
    basándose en sus valoraciones previas o lo asigna a uno si es nuevo
    """
    if usuario_uri in usuarios_perfiles:
        perfil_nombre = usuarios_perfiles[usuario_uri]
        return perfiles_colaborativos[perfil_nombre]
    
    # Si es nuevo, buscar valoraciones previas para determinar su perfil
    valoraciones = {}
    for val in g_store.subjects(RDF.type, onto.Valoracion):
        if (val, onto.deUsuario, Literal(usuario_uri)) in g_store:
            plan = g_store.value(subject=val, predicate=onto.sobrePlan)
            if plan:
                # Buscar actividades del plan
                for actividad in g_store.objects(subject=plan, predicate=onto.seRealizan):
                    # Buscar tipo de actividad
                    for tipo in ['Cultural', 'Aventura', 'Gastronomica', 'Naturaleza', 'Exterior', 'Interior']:
                        tipo_uri = getattr(onto, tipo, None)
                        if tipo_uri and (actividad, RDF.type, tipo_uri) in g_store:
                            valoraciones[tipo] = valoraciones.get(tipo, 0) + 1
    
    # Determinar perfil según sus valoraciones
    if valoraciones:
        perfil_principal = max(valoraciones.items(), key=lambda x: x[1])[0]
        if perfil_principal == 'Cultural':
            perfil_nombre = 'Culturales'
        elif perfil_principal in ['Aventura', 'Exterior']:
            perfil_nombre = 'Aventureros' 
        elif perfil_principal == 'Gastronomica':
            perfil_nombre = 'Gastronómicos'
        elif perfil_principal in ['Naturaleza']:
            perfil_nombre = 'Naturalistas'
        else:
            # Asignación por defecto para casos no claros
            perfil_nombre = random.choice(['Culturales', 'Aventureros', 'Gastronómicos', 'Naturalistas'])
    else:
        # Si no hay valoraciones, asignar aleatoriamente
        perfil_nombre = random.choice(['Culturales', 'Aventureros', 'Gastronómicos', 'Naturalistas'])
    
    # Registrar el usuario en el perfil asignado
    usuarios_perfiles[usuario_uri] = perfil_nombre
    perfiles_colaborativos[perfil_nombre].añadir_usuario(usuario_uri)
    logger.info(f"Usuario {usuario_uri} asignado al perfil colaborativo: {perfil_nombre}")
    
    return perfiles_colaborativos[perfil_nombre]

# Flask
app = Flask(__name__)
mss_cnt = 0

@app.route("/comm")
def comunicacion():
    global mss_cnt
    global g_valoraciones

    message = request.args['content']
    gm = Graph()
    gm.parse(data=message, format='xml')

    props = get_message_properties(gm)
    if props['performative'] != ACL.request:
        return Response(status=400)

    content = props['content']
    tipo = gm.value(subject=content, predicate=RDF.type)

    # Manejo de peticiones del AgenteMantenedor
    if tipo == onto.NotificacionPlanTerminado:
        plan = gm.value(subject=content, predicate=onto.planAValorar)
        usuario = gm.value(subject=content, predicate=onto.paraUsuario)
        
        # Registrar solicitud de valoración
        g_valoraciones.add((plan, RDF.type, onto.PlanPendienteValoracion))  # Añadir tipo específico
        g_valoraciones.add((plan, onto.valoracionSolicitada, Literal(True)))
        g_valoraciones.add((plan, onto.usuario, usuario))  # Añadir el usuario directamente
        g_valoraciones.add((plan, onto.fechaSolicitudValoracion, 
                          Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
        
        guardar_valoraciones()
        
        logger.info(f"Solicitud recibida del AgenteMantenedor para valorar plan {plan} por usuario {usuario}")
        
        # Generar enlace para valoración
        enlace_valoracion = f"http://{hostname}:{port}/form_valoracion?plan={plan}&usuario={usuario}"
        logger.info(f"Enlace de valoración: {enlace_valoracion}")
        
        # Confirmar recepción al AgenteMantenedor
        g_resp = Graph()
        resp_id = URIRef(f"http://www.semanticweb.org/ontologia/confirmacion/{uuid.uuid4()}")
        g_resp.add((resp_id, RDF.type, onto.ConfirmacionSolicitudValoracion))
        g_resp.add((resp_id, onto.planConfirmado, plan))
        
        mss_cnt += 1
        return Response(build_message(g_resp, ACL.inform, 
                                    sender=AgenteValoraciones.uri, 
                                    receiver=props['sender'], 
                                    content=resp_id,
                                    msgcnt=mss_cnt).serialize(format='xml'),
                      mimetype='text/xml')

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
    perfil_info = None
    estadisticas_perfiles = {}

    # Preparar estadísticas de perfiles colaborativos
    for nombre, perfil in perfiles_colaborativos.items():
        estadisticas_perfiles[nombre] = {
            'usuarios': len(perfil.usuarios),
            'preferencia_principal': perfil.calcular_prioridad_actividad() or "Sin datos",
            'ciudades_top': sorted(perfil.ciudades_populares.items(), key=lambda x: x[1], reverse=True)[:3] if perfil.ciudades_populares else []
        }

    if request.method == 'POST':
        usuario = request.form['usuario']
        perfil = obtener_perfil_usuario(usuario)
        perfil_info = {
            'nombre': perfil.nombre,
            'usuarios_similares': len(perfil.usuarios),
            'preferencia_actividad': perfil.calcular_prioridad_actividad(),
            'ciudades_visitadas': sorted(perfil.ciudades_populares.items(), key=lambda x: x[1], reverse=True)
        }

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

    return render_template("valoraciones_admin.html", planes=planes, valoraciones=valoraciones, recomendacion=recomendacion, perfil_info=perfil_info, estadisticas_perfiles=estadisticas_perfiles)


def procesar_valoracion(usuario, plan, receiver):
    global mss_cnt

    # Obtener perfil colaborativo del usuario
    perfil = obtener_perfil_usuario(str(usuario))
    
    # Extraer destino del plan
    destino = g_store.value(subject=plan, predicate=onto.llegaA)
    if destino:
        ciudad = g_store.value(subject=destino, predicate=onto.NombreCiudad)
        if ciudad:
            # Registrar ciudad en el perfil colaborativo
            perfil.registrar_ciudad(str(ciudad))
    
    # Generar valoración
    id_val = URIRef(f"http://www.semanticweb.org/ontologia/valoracion/{uuid.uuid4()}")
    puntuacion = random.randint(2, 5)  # Tendencia más positiva para simulación
    comentarios = ["Excelente", "Muy buena", "Buena", "Aceptable", "Mejorable"]
    comentario = comentarios[5 - puntuacion] if puntuacion <= 5 else "Excelente"

    # Guardar en el grafo
    g_store.add((id_val, RDF.type, onto.Valoracion))
    g_store.add((id_val, onto.deUsuario, usuario))
    g_store.add((id_val, onto.sobrePlan, plan))
    g_store.add((id_val, onto.puntuacion, Literal(puntuacion, datatype=XSD.integer)))
    g_store.add((id_val, RDFS.comment, Literal(comentario)))
    g_store.add((id_val, onto.fechaValoracion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
    
    # Actualizar perfil con las actividades del plan
    for actividad in g_store.objects(subject=plan, predicate=onto.seRealizan):
        for tipo in ['Cultural', 'Aventura', 'Gastronomica', 'Naturaleza', 'Exterior', 'Interior']:
            tipo_uri = getattr(onto, tipo, None)
            if tipo_uri and (actividad, RDF.type, tipo_uri) in g_store:
                # Actualizar las preferencias del perfil colaborativo
                perfil.actualizar_preferencias(tipo, puntuacion)
                perfil.registrar_valoracion(tipo, puntuacion)
    
    # Preparar respuesta
    g_res = Graph()
    g_res.bind("onto", onto)
    g_res.add((id_val, RDF.type, onto.RespuestaValoracion))
    g_res += g_store.triples((id_val, None, None))

    mss_cnt += 1
    return Response(build_message(g_res, ACL.inform, AgenteValoraciones.uri, receiver, mss_cnt).serialize(format='xml'), mimetype='text/xml')

def procesar_peticion_recomendacion(usuario_uri, receptor_uri):
    global mss_cnt
    
    # Asegurar que tenemos los datos más recientes
    cargar_planes()
    
    # Obtener el perfil colaborativo del usuario
    perfil = obtener_perfil_usuario(usuario_uri)
    
    # Buscar ciudades ya visitadas por este usuario específico
    ciudades_visitadas = set()
    for val in g_valoraciones.subjects(RDF.type, onto.Valoracion):
        if (val, onto.deUsuario, Literal(usuario_uri)) in g_valoraciones:
            plan = g_valoraciones.value(subject=val, predicate=onto.sobrePlan)
            if plan:
                # Buscamos en el grafo de planes
                destino = g_planes.value(subject=plan, predicate=onto.llegaA)
                if destino:
                    ciudad = g_planes.value(subject=destino, predicate=onto.NombreCiudad)
                    if ciudad:
                        ciudades_visitadas.add(str(ciudad))
    
    # Intentar obtener recomendación del perfil colaborativo
    destino_recomendado = perfil.calcular_ciudad_recomendada(ciudades_visitadas)
    
    # Si no hay recomendación del perfil, usar método alternativo
    if not destino_recomendado:
        # Buscar destinos en la ontología
        destinos_definidos = set()
        for dest in g_store.subjects(RDF.type, onto.Ciudad):
            nombre = g_store.value(subject=dest, predicate=onto.NombreCiudad)
            if nombre:
                destinos_definidos.add(str(nombre))
        
        # Filtrar no visitados
        destinos_no_visitados = list(destinos_definidos - ciudades_visitadas)
        
        if destinos_no_visitados:
            destino_recomendado = random.choice(destinos_no_visitados)
        else:
            destino_recomendado = random.choice(list(destinos_definidos)) if destinos_definidos else "Barcelona"
    
    # Tipo de actividad preferida para este perfil
    tipo_preferido = perfil.calcular_prioridad_actividad()
    
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
    
    # Añadir justificación basada en el perfil colaborativo
    justificacion = f"Recomendación basada en preferencias del grupo '{perfil.nombre}'"
    if tipo_preferido:
        justificacion += f" con afinidad por actividades de tipo {tipo_preferido}"
    
    g.add((recomendacion_uri, RDFS.comment, Literal(justificacion)))

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
    """
    Hilo que busca planes finalizados y solicita valoración al usuario
    """
    while True:
        for plan in g_store.subjects(RDF.type, onto.Plan):
            estado = g_store.value(plan, onto.estado)
            if estado and str(estado) == 'finalizado':
                valorado = g_store.value(plan, onto.valoracionSolicitada)
                if not valorado:
                    # Registrar que se solicitó la valoración
                    g_store.add((plan, onto.valoracionSolicitada, Literal(True)))
                    
                    # Obtener usuario
                    usuario = g_store.value(plan, onto.usuario)
                    if usuario:
                        # Generar enlace para valoración
                        enlace_valoracion = f"http://{hostname}:{port}/form_valoracion?plan={plan}&usuario={usuario}"
                        
                        logger.info(f"Solicitando valoración para plan {plan}")
                        logger.info(f"Enlace de valoración: {enlace_valoracion}")
                        
                        # Aquí podrías enviar un correo electrónico o notificación al usuario 
                        # con el enlace para valorar
        time.sleep(60)

@app.route("/valorar", methods=['POST'])
def valorar_plan():
    """
    Endpoint para que los usuarios envíen valoraciones de planes realizados
    """
    try:
        plan_uri = request.form.get('plan_uri')
        usuario_uri = request.form.get('usuario_uri')
        puntuacion = int(request.form.get('puntuacion', 0))
        comentario = request.form.get('comentario', '')
        
        if not plan_uri or not usuario_uri or puntuacion < 1 or puntuacion > 5:
            return "Error: Datos incompletos o inválidos", 400
        
        # Procesar la valoración con los datos proporcionados por el usuario
        procesar_valoracion_usuario(
            URIRef(usuario_uri), 
            URIRef(plan_uri), 
            puntuacion, 
            comentario
        )
        
        return render_template("valoracion_exitosa.html", plan=plan_uri)
    except Exception as e:
        logger.error(f"Error al procesar valoración: {e}")
        return f"Error al procesar valoración: {e}", 500

@app.route("/form_valoracion", methods=['GET'])
def mostrar_formulario_valoracion():
    """
    Muestra un formulario para que el usuario valore un plan
    """
    plan_uri = request.args.get('plan')
    usuario_uri = request.args.get('usuario')
    
    if not plan_uri or not usuario_uri:
        return "Error: Parámetros faltantes", 400
    
    # Cargar planes desde el grafo externo
    cargar_planes()
    
    # Obtener detalles del plan para mostrar al usuario
    destino = g_planes.value(subject=URIRef(plan_uri), predicate=onto.llegaA)
    destino_nombre = g_planes.value(subject=destino, predicate=onto.NombreCiudad) if destino else "Desconocido"
    
    actividades = []
    for actividad in g_planes.objects(subject=URIRef(plan_uri), predicate=onto.seRealizan):
        nombre_actividad = g_planes.value(subject=actividad, predicate=RDFS.label) or "Actividad sin nombre"
        tipo_actividad = None
        for tipo in ['Cultural', 'Aventura', 'Gastronomica', 'Naturaleza', 'Exterior', 'Interior']:
            tipo_uri = getattr(onto, tipo, None)
            if tipo_uri and (actividad, RDF.type, tipo_uri) in g_planes:
                tipo_actividad = tipo
                break
        
        actividades.append({
            'nombre': str(nombre_actividad),
            'tipo': tipo_actividad or "Desconocido"
        })
    
    # Obtener más detalles del plan para personalizar
    fecha_inicio = g_planes.value(subject=URIRef(plan_uri), predicate=onto.fechaInicio)
    fecha_fin = g_planes.value(subject=URIRef(plan_uri), predicate=onto.fechaFin)
    
    return render_template(
        "valoracion_form.html",
        usuario_uri=usuario_uri,
        plan_uri=plan_uri,
        destino=str(destino_nombre),
        actividades=actividades,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin
    )

def procesar_valoracion_usuario(usuario, plan, puntuacion, comentario):
    """
    Procesa una valoración proporcionada por el usuario y la almacena en el grafo de valoraciones
    """
    global mss_cnt
    
    # Obtener detalles del plan desde el grafo de planes
    cargar_planes()
    
    # Obtener perfil colaborativo del usuario o asignar uno nuevo
    perfil = obtener_perfil_usuario(str(usuario))
    logger.info(f"Usuario {usuario} asociado al perfil: {perfil.nombre}")
    
    # Extraer destino del plan
    destino = g_planes.value(subject=plan, predicate=onto.llegaA)
    if destino:
        ciudad = g_planes.value(subject=destino, predicate=onto.NombreCiudad)
        if ciudad:
            # Registrar ciudad en el perfil colaborativo
            perfil.registrar_ciudad(str(ciudad))
            logger.info(f"Ciudad visitada registrada: {ciudad} para perfil {perfil.nombre}")
    
    # Crear la valoración en el sistema
    id_val = URIRef(f"http://www.semanticweb.org/ontologia/valoracion/{uuid.uuid4()}")
    
    # Guardar en el grafo de VALORACIONES (no en g_store)
    g_valoraciones.add((id_val, RDF.type, onto.Valoracion))
    g_valoraciones.add((id_val, onto.deUsuario, usuario))
    g_valoraciones.add((id_val, onto.sobrePlan, plan))
    g_valoraciones.add((id_val, onto.puntuacion, Literal(puntuacion, datatype=XSD.integer)))
    g_valoraciones.add((id_val, RDFS.comment, Literal(comentario)))
    g_valoraciones.add((id_val, onto.fechaValoracion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
    
    # Marcar el plan como valorado
    g_valoraciones.add((plan, onto.valoracionGenerada, Literal(True)))
    
    logger.info(f"Valoración registrada en BD Valoraciones: ID={id_val}, Puntuación={puntuacion}/5")
    
    # Actualizar perfil with las actividades del plan
    tipos_encontrados = []
    for actividad in g_planes.objects(subject=plan, predicate=onto.seRealizan):
        for tipo in ['Cultural', 'Aventura', 'Gastronomica', 'Naturaleza', 'Exterior', 'Interior']:
            tipo_uri = getattr(onto, tipo, None)
            if tipo_uri and (actividad, RDF.type, tipo_uri) in g_planes:
                # Actualizar las preferencias del perfil colaborativo
                perfil.actualizar_preferencias(tipo, puntuacion)
                perfil.registrar_valoracion(tipo, puntuacion)
                tipos_encontrados.append(tipo)
    
    if tipos_encontrados:
        logger.info(f"Tipos de actividades valoradas: {', '.join(tipos_encontrados)}")
    
    # Recalcular perfil del usuario basado en sus valoraciones
    recalcular_perfil_usuario(str(usuario))
    
    # Guardar cambios en el archivo de valoraciones
    guardar_valoraciones()
    
    return id_val

def recalcular_perfil_usuario(usuario_uri):
    """
    Recalcula el perfil al que debe pertenecer un usuario basado en sus valoraciones reales
    """
    # Contar valoraciones por tipo de actividad
    valoraciones_por_tipo = {}
    
    # Buscar todas las valoraciones del usuario
    for val in g_store.subjects(RDF.type, onto.Valoracion):
        if (val, onto.deUsuario, URIRef(usuario_uri)) in g_store:
            puntuacion = g_store.value(subject=val, predicate=onto.puntuacion)
            plan = g_store.value(subject=val, predicate=onto.sobrePlan)
            
            if not plan or not puntuacion:
                continue
                
            # Solo considerar valoraciones positivas (4-5)
            if int(puntuacion) < 4:
                continue
                
            # Buscar actividades del plan
            for actividad in g_store.objects(subject=plan, predicate=onto.seRealizan):
                # Determinar tipo de actividad
                for tipo in ['Cultural', 'Aventura', 'Gastronomica', 'Naturaleza', 'Exterior', 'Interior']:
                    tipo_uri = getattr(onto, tipo, None)
                    if tipo_uri and (actividad, RDF.type, tipo_uri) in g_store:
                        valoraciones_por_tipo[tipo] = valoraciones_por_tipo.get(tipo, 0) + int(puntuacion)
    
    # Si hay suficientes valoraciones, determinar el perfil más apropiado
    if valoraciones_por_tipo:
        # Determinar la categoría predominante
        perfil_principal = max(valoraciones_por_tipo.items(), key=lambda x: x[1])[0]
        
        # Mapear tipo de actividad a perfil
        nuevo_perfil = None
        if perfil_principal == 'Cultural':
            nuevo_perfil = 'Culturales'
        elif perfil_principal in ['Aventura', 'Exterior']:
            nuevo_perfil = 'Aventureros'
        elif perfil_principal in ['Gastronomica']:
            nuevo_perfil = 'Gastronómicos'
        elif perfil_principal in ['Naturaleza']:
            nuevo_perfil = 'Naturalistas'
        else:
            return  # Mantener perfil actual
        
        # Verificar si es necesario cambiar de perfil
        perfil_actual = usuarios_perfiles.get(usuario_uri)
        if nuevo_perfil and nuevo_perfil != perfil_actual:
            # Quitar del perfil anterior
            if perfil_actual:
                perfiles_colaborativos[perfil_actual].usuarios.discard(usuario_uri)
            
            # Añadir al nuevo perfil
            usuarios_perfiles[usuario_uri] = nuevo_perfil
            perfiles_colaborativos[nuevo_perfil].añadir_usuario(usuario_uri)
            logger.info(f"Usuario {usuario_uri} reasignado al perfil: {nuevo_perfil} (antes: {perfil_actual})")

    
def valorar_capacidad():
    """
    Monitoreo de solicitudes de valoración
    
    Esta capacidad ahora complementa al AgenteMantenedor, revisando
    solicitudes ya recibidas y enviando recordatorios cuando sea necesario.
    """
    logger.info("Monitoreando solicitudes de valoración pendientes")
    
    # Cargar datos actualizados antes de procesar
    if os.path.exists("databases/valoraciones.rdf"):
        # Limpiar datos existentes para evitar duplicados
        g_valoraciones.remove((None, onto.valoracionSolicitada, None))
        g_valoraciones.parse("databases/valoraciones.rdf", format="xml")
    
    # Cargar planes desde el archivo
    cargar_planes()
    
    # Contar solicitudes pendientes (usando el tipo específico)
    solicitudes_pendientes = 0
    
    # Buscar por PlanPendienteValoracion y/o valoracionSolicitada=True
    for plan, _, _ in g_valoraciones.triples((None, RDF.type, onto.PlanPendienteValoracion)):
        # Verificar si ya ha sido valorado
        ya_valorado = False
        for val in g_valoraciones.subjects(RDF.type, onto.Valoracion):
            if (val, onto.sobrePlan, plan) in g_valoraciones:
                ya_valorado = True
                break
        
        if not ya_valorado:
            solicitudes_pendientes += 1
    
    # También buscar por valoracionSolicitada=True (para compatibilidad)
    for plan, _, _ in g_valoraciones.triples((None, onto.valoracionSolicitada, Literal(True))):
        # Verificar si ya fue contado o valorado
        if (plan, RDF.type, onto.PlanPendienteValoracion) not in g_valoraciones:
            ya_valorado = False
            for val in g_valoraciones.subjects(RDF.type, onto.Valoracion):
                if (val, onto.sobrePlan, plan) in g_valoraciones:
                    ya_valorado = True
                    break
            
            if not ya_valorado:
                solicitudes_pendientes += 1
    
    logger.info(f"Solicitudes de valoración pendientes: {solicitudes_pendientes}")

def recomendar_actividad_capacidad():
    """
    Recomendación de actividades específicas basada en perfiles de usuario
    
    Esta capacidad analiza el perfil colaborativo del usuario y recomienda
    actividades específicas que coincidan con sus preferencias.
    """
    logger.info("Activando capacidad: Recomendar actividades")
    
    # Cargar planes y actividades actualizados
    cargar_planes()
    
    # Buscar usuarios que hayan valorado algún plan
    usuarios_activos = set()
    for val in g_valoraciones.subjects(RDF.type, onto.Valoracion):
        usuario = g_valoraciones.value(val, onto.deUsuario)
        if usuario:
            usuarios_activos.add(str(usuario))
    
    # Seleccionar hasta 3 usuarios para enviar recomendaciones
    usuarios_seleccionados = random.sample(list(usuarios_activos), min(3, len(usuarios_activos))) if usuarios_activos else []
    
    for usuario in usuarios_seleccionados:
        try:
            # Obtener perfil del usuario
            perfil = obtener_perfil_usuario(usuario)
            
            # Determinar el tipo de actividad preferido
            tipo_preferido = perfil.calcular_prioridad_actividad()
            if not tipo_preferido:
                continue
                
            logger.info(f"Buscando actividades de tipo '{tipo_preferido}' para usuario {usuario} (Perfil: {perfil.nombre})")
            
            # Recuperar actividades ya realizadas por el usuario
            actividades_realizadas = set()
            for val in g_valoraciones.subjects(RDF.type, onto.Valoracion):
                if (val, onto.deUsuario, Literal(usuario)) in g_valoraciones:
                    plan = g_valoraciones.value(subject=val, predicate=onto.sobrePlan)
                    if plan:
                        # Extraer todas las actividades de los planes valorados
                        for act in g_planes.objects(subject=plan, predicate=onto.seRealizan):
                            actividades_realizadas.add(str(act))
            
            # Buscar actividades del tipo preferido que no haya realizado
            actividades_recomendables = []
            tipo_uri = getattr(onto, tipo_preferido)
            
            for actividad in g_planes.subjects(RDF.type, tipo_uri):
                # Verificar que no la haya realizado antes
                if str(actividad) not in actividades_realizadas:
                    nombre = g_planes.value(subject=actividad, predicate=RDFS.label) or "Actividad sin nombre"
                    descripcion = g_planes.value(subject=actividad, predicate=RDFS.comment) or ""
                    
                    # Obtener la ciudad de la actividad
                    ciudad = None
                    for s, p, o in g_planes.triples((actividad, onto.sehaceEn, None)):
                        ciudad_nombre = g_planes.value(subject=o, predicate=onto.NombreCiudad)
                        if ciudad_nombre:
                            ciudad = str(ciudad_nombre)
                            break
                    
                    actividades_recomendables.append({
                        'uri': actividad,
                        'nombre': str(nombre),
                        'descripcion': str(descripcion),
                        'tipo': tipo_preferido,
                        'ciudad': ciudad or "Desconocida"
                    })
            
            # Si no hay actividades recomendables, continuar con el siguiente usuario
            if not actividades_recomendables:
                logger.info(f"No se encontraron actividades nuevas de tipo {tipo_preferido} para recomendar al usuario {usuario}")
                continue
                
            # Seleccionar una actividad aleatoria de las disponibles
            actividad_recomendada = random.choice(actividades_recomendables)
            
            # Crear recomendación en RDF
            g = Graph()
            g.bind("onto", onto)
            g.bind("rdf", RDF)
            g.bind("rdfs", RDFS)
            
            recomendacion_id = URIRef(f"http://www.semanticweb.org/ontologia/recomendacion_actividad/{uuid.uuid4()}")
            g.add((recomendacion_id, RDF.type, onto.RecomendacionActividad))
            g.add((recomendacion_id, onto.paraUsuario, Literal(usuario)))
            g.add((recomendacion_id, onto.actividadRecomendada, actividad_recomendada['uri']))
            g.add((recomendacion_id, RDFS.label, Literal(f"Recomendación: {actividad_recomendada['nombre']}")))
            g.add((recomendacion_id, RDFS.comment, Literal(f"Creemos que te gustaría esta actividad de tipo {actividad_recomendada['tipo']} en {actividad_recomendada['ciudad']}")))
            g.add((recomendacion_id, onto.fechaRecomendacion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
            
            # Mostrar información detallada por consola
            print("\n=============================================================")
            print(f"RECOMENDACIÓN DE ACTIVIDAD PARA USUARIO: {usuario}")
            print("=============================================================")
            print(f"Perfil: {perfil.nombre}")
            print(f"Preferencia de actividad: {tipo_preferido}")
            print(f"Actividad recomendada: {actividad_recomendada['nombre']}")
            print(f"Ciudad: {actividad_recomendada['ciudad']}")
            print(f"Tipo: {actividad_recomendada['tipo']}")
            print("=============================================================\n")
            
            # Registrar la recomendación en el sistema como antes
            g_store.add((recomendacion_id, RDF.type, onto.RecomendacionActividadEnviada))
            g_store.add((recomendacion_id, onto.paraUsuario, Literal(usuario)))
            g_store.add((recomendacion_id, onto.actividad, actividad_recomendada['uri']))
            g_store.add((recomendacion_id, onto.fechaEnvio, Literal(datetime.datetime.now().isoformat())))
            
            logger.info(f"Recomendada actividad '{actividad_recomendada['nombre']}' de tipo {tipo_preferido} a usuario {usuario}")
            
            
        except Exception as e:
            logger.error(f"Error al generar recomendación de actividad: {e}")
            traceback.print_exc()

def agentbehavior2(cola):
    """
    Comportamiento del agente - Generación de recomendaciones
    """


    logger.info("Cargando valoraciones y perfiles de usuario antes de iniciar recomendaciones...")
    cargar_valoraciones()
    cargar_planes()
    
    # Verificar que se hayan cargado correctamente
    num_valoraciones = len(list(g_valoraciones.subjects(RDF.type, onto.Valoracion)))
    num_planes = len(list(g_planes.subjects(RDF.type, onto.Plan)))
    logger.info(f"Se han cargado {num_valoraciones} valoraciones y {num_planes} planes antes de iniciar")
    
    # Esperar un poco antes de empezar para dar tiempo a que se procesen valoraciones
    time.sleep(15)
    
    # Bucle principal del comportamiento
    while True:
        try:
            # Verificar si hay un mensaje en la cola
            try:
                msg = cola.get_nowait()
                if msg == 0:
                    logger.info("Finalizando comportamiento del agente")
                    break
            except:
                pass  # No hay mensajes, continuar
            
            recomendar_actividad_capacidad()
            tiempo_espera = TIEMPO_ENTRE_RECOMENDACIONES_ACTIVIDAD

            # Esperar antes del siguiente ciclo
            time.sleep(TIEMPO_ENTRE_RECOMENDACIONES_ACTIVIDAD)
        except Exception as e:
            logger.error(f"Error en comportamiento de recomendaciones: {e}")
            time.sleep(5)  # Esperar un poco antes de reintentar en caso de error

@app.route("/test")
def test():
    """Interfaz de prueba que permite activar manualmente las capacidades"""
    # Forzar carga de planes y valoraciones
    cargar_planes()
    if os.path.exists("databases/valoraciones.rdf"):
        g_valoraciones.parse("databases/valoraciones.rdf", format="xml")
    
    # Verificar valoraciones existentes
    valoraciones = list(g_valoraciones.subjects(RDF.type, onto.Valoracion))
    num_valoraciones = len(valoraciones)
    
    # Count planes
    planes = list(g_planes.subjects(RDF.type, onto.Plan))
    num_planes = len(planes)
    
    # Count users with profiles
    num_usuarios = len(usuarios_perfiles)
    
    # Count recommendations sent
    recomendaciones = list(g_store.subjects(RDF.type, onto.RecomendacionEnviada))
    num_recomendaciones = len(recomendaciones)
    
    # Obtener planes pendientes de valoración
    planes_pendientes = []
    
    # Buscar por PlanPendienteValoracion o valoracionSolicitada=True
    solicitudes = []
    solicitudes.extend(g_valoraciones.subjects(RDF.type, onto.PlanPendienteValoracion))
    
    for plan, _, _ in g_valoraciones.triples((None, onto.valoracionSolicitada, Literal(True))):
        if plan not in solicitudes:
            solicitudes.append(plan)
    
    for plan in solicitudes:
        # Verificar si ya fue valorado
        ya_valorado = False
        for val in g_valoraciones.subjects(RDF.type, onto.Valoracion):
            if (val, onto.sobrePlan, plan) in g_valoraciones:
                ya_valorado = True
                break
        
        if not ya_valorado:
            # Usar el usuario guardado en g_valoraciones, no en g_planes
            usuario = g_valoraciones.value(plan, onto.usuario)
            if not usuario and plan in g_planes:
                usuario = g_planes.value(plan, onto.usuario) or g_planes.value(plan, onto.esRealizadoPor)
            
            # Extraer información del plan
            destino_uri = g_planes.value(plan, onto.llegaA)
            destino = g_planes.value(destino_uri, onto.NombreCiudad) if destino_uri else "Desconocido"
            
            if usuario:
                planes_pendientes.append({
                    'uri': plan,
                    'usuario': usuario,
                    'destino': destino,
                    'enlace': f"/form_valoracion?plan={plan}&usuario={usuario}"
                })
    
    return f'''
    <html>
        <head>
            <title>Test AgenteValoraciones</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                h1 {{ color: #3498db; }}
                .button {{ padding: 10px; background: #3498db; color: white; text-decoration: none; border-radius: 5px; display: inline-block; margin: 5px; border: none; cursor: pointer; }}
                .info {{ background: #f8f9fa; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
                .planes-list {{ margin: 20px 0; }}
                .plan-item {{ background: #eee; padding: 10px; margin: 5px 0; border-radius: 5px; }}
                .valorar-btn {{ background: #27ae60; color: white; text-decoration: none; padding: 5px 10px; border-radius: 3px; font-size: 0.9em; }}
            </style>
        </head>
        <body>
            <h1>Agente de Valoraciones</h1>
            
            <div class="info">
                <p>Planes cargados: {num_planes}</p>
                <p>Usuarios con perfil: {num_usuarios}</p>
                <p>Valoraciones registradas: {num_valoraciones}</p>
                <p>Recomendaciones enviadas: {num_recomendaciones}</p>
                <p>Frecuencia de lectura de planes: {TIEMPO_ENTRE_LECTURAS_PLANES} segundos</p>
                <p>Frecuencia de solicitud de valoraciones: {TIEMPO_ENTRE_VALORACIONES} segundos</p>
                <p>Frecuencia de recomendaciones: {TIEMPO_ENTRE_RECOMENDACIONES_ACTIVIDAD} segundos</p>
            </div>
            
            <h2>Planes pendientes de valorar ({len(planes_pendientes)})</h2>
            <div class="planes-list">
                {'' if not planes_pendientes else ''.join([
                    f"<div class='plan-item'>"
                    f"<p><strong>Plan:</strong> {plan['uri']}</p>"
                    f"<p><strong>Usuario:</strong> {plan['usuario']}</p>"
                    f"<p><strong>Destino:</strong> {plan['destino']}</p>"
                    f"<p><a href='{plan['enlace']}' class='valorar-btn'>Valorar este plan</a></p>"
                    f"</div>"
                    for plan in planes_pendientes
                ])}
            </div>
            
            <h2>Acciones</h2>
            <form action="/admin" method="GET">
                <button type="submit" class="button">Panel de Administración</button>
            </form>
            
            <h2>Activación manual de capacidades</h2>
            <form action="/activar_capacidad" method="POST">
                <input type="hidden" name="capacidad" value="valorar">
                <button type="submit" class="button">Activar capacidad: Valorar planes</button>
            </form>
            
            <form action="/activar_capacidad" method="POST">
                <input type="hidden" name="capacidad" value="recomendar">
                <button type="submit" class="button">Activar capacidad: Recomendar viajes</button>
            </form>
            
            <form action="/activar_capacidad" method="POST">
                <input type="hidden" name="capacidad" value="recomendar_actividad">
                <button type="submit" class="button">Activar capacidad: Recomendar actividades</button>
            </form>

            <h2>Probar recomendación por perfil específico</h2>
            <p>Selecciona un perfil para crear un usuario de prueba y ver recomendaciones:</p>
            <p>
                <a href="/test_recomendar_usuario?perfil=Culturales" class="button">Usuario Cultural</a>
                <a href="/test_recomendar_usuario?perfil=Aventureros" class="button">Usuario Aventurero</a>
                <a href="/test_recomendar_usuario?perfil=Gastronómicos" class="button">Usuario Gastronómico</a>
                <a href="/test_recomendar_usuario?perfil=Naturalistas" class="button">Usuario Naturalista</a>
            </p>
        </body>
    </html>
    '''

@app.route("/activar_capacidad", methods=['POST'])
def activar_capacidad():
    """
    Activa manualmente una capacidad para pruebas
    """
    capacidad = request.form.get('capacidad')
    
    if capacidad == 'valorar':
        # Activar capacidad de valoración
        valorar_capacidad()
        return f'''
        <html>
            <head>
                <title>Capacidad Activada</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .success {{ background-color: #d4edda; color: #155724; padding: 15px; border-radius: 5px; }}
                </style>
                <meta http-equiv="refresh" content="3;url=/test">
            </head>
            <body>
                <div class="success">
                    <h1>Capacidad de Valoración Activada</h1>
                    <p>La capacidad ha sido activada correctamente.</p>
                    <p>Redirigiendo al panel de pruebas...</p>
                </div>
            </body>
        </html>
        '''
    elif capacidad == 'recomendar_actividad':
        # Activar capacidad de recomendación de actividades
        recomendar_actividad_capacidad()
        return f'''
        <html>
            <head>
                <title>Capacidad Activada</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .success {{ background-color: #d4edda; color: #155724; padding: 15px; border-radius: 5px; }}
                </style>
                <meta http-equiv="refresh" content="3;url=/test">
            </head>
            <body>
                <div class="success">
                    <h1>Capacidad de Recomendación de Actividades Activada</h1>
                    <p>La capacidad ha sido activada correctamente.</p>
                    <p>Redirigiendo al panel de pruebas...</p>
                </div>
            </body>
        </html>
        '''
    
    return "Capacidad no reconocida", 400

g_planes = Graph()  # Grafo para almacenar planes leídos desde AgentePlanes
g_valoraciones = Graph()  # Grafo para almacenar las valoraciones

# Función para guardar valoraciones (solo mantiene el grafo en memoria)
def guardar_valoraciones():
    """
    Guarda las valoraciones en un archivo RDF
    """
    try:
        # Preparar el grafo para guardar
        g = Graph()
        g.bind('rdf', RDF)
        g.bind('rdfs', RDFS)
        g.bind('onto', onto)
        g.bind('xsd', XSD)
        
        # Copiar todas las valoraciones al grafo de guardado
        for s, p, o in g_valoraciones:
            g.add((s, p, o))
            
        # Guardar en archivo
        g.serialize("databases/valoraciones.rdf", format="xml")
        
        num_valoraciones = len(list(g_valoraciones.subjects(RDF.type, onto.Valoracion)))
        logger.info(f"Base de datos de valoraciones guardada con {num_valoraciones} valoraciones")
        return True
    except Exception as e:
        logger.error(f"Error al guardar valoraciones: {e}")
        return False

# Función para cargar los planes desde el grafo externo
def cargar_planes():
    """
    Carga los planes directamente desde el archivo planes_activos.rdf
    """
    try:
        # Comprobar si existe el archivo
        if os.path.exists("databases/planes_activos.rdf"):
            # Limpiar el grafo antes de cargar nuevos datos 
            g_planes.remove((None, None, None))
            
            # Cargar planes desde el archivo RDF
            g_planes.parse("databases/planes_activos.rdf", format="xml")

            # Contar los planes para logging
            num_planes = len(list(g_planes.subjects(RDF.type, onto.Plan)))
            num_finalizados = len(list(s for s in g_planes.subjects(RDF.type, onto.Plan) 
                                    if (s, onto.estado, Literal("finalizado")) in g_planes))
            
            logger.info(f"Planes cargados desde planes_activos.rdf: {num_planes} planes, {num_finalizados} finalizados")
            return True
        else:
            logger.warning("No se encontró el archivo planes_activos.rdf")
            return False
            
    except Exception as e:
        logger.error(f"Error al cargar planes desde archivo: {e}")
        traceback.print_exc()
        return False

def buscar_agente_por_tipo(tipo_agente):
    """
    Busca un agente por su tipo en el directorio
    
    :param tipo_agente: Tipo del agente a buscar (DSO.TransportAgent, DSO.SolverAgent, etc)
    :return: Información del agente o None si no se encuentra
    """
    global mss_cnt

    tipo_str = str(tipo_agente).split('#')[-1]
    logger.info(f"Buscando agente de tipo {tipo_str} en el directorio...")
    
    # Crear grafo para buscar en el directorio
    gmess = Graph()
    gmess.bind('dso', DSO)
    gmess.bind('rdf', RDF)
    
    search_obj = agn[f'search-{str(uuid.uuid4())}']
    gmess.add((search_obj, RDF.type, DSO.Search))
    gmess.add((search_obj, DSO.AgentType, tipo_agente))
    
    # Construir el mensaje
    msg = build_message(gmess, ACL.request,
                       sender=AgenteValoraciones.uri,
                       receiver=DirectoryAgent.uri,
                       content=search_obj,
                       msgcnt=mss_cnt)
    mss_cnt += 1
    
    # Mejor manejo de errores
    try:
        # Enviar el mensaje
        gr = send_message(msg, DirectoryAgent.address)
        
        # Verificación básica de la respuesta
        if not isinstance(gr, Graph):
            logger.error(f"La respuesta del directorio no es un grafo válido: {gr}")
            return None
        
        # Procesar la respuesta
        msg = gr.value(predicate=RDF.type, object=ACL.FipaAclMessage)
        if not msg:
            logger.error("No se encontró un mensaje FIPA ACL en la respuesta")
            return None
            
        content = gr.value(subject=msg, predicate=ACL.content)
        if not content:
            logger.error("No se encontró el contenido del mensaje")
            return None
        
        # Buscar todos los agentes en la respuesta
        agentes_encontrados = []
        
        # Método 1: Buscar por AgentType
        for s, p, o in gr.triples((None, DSO.AgentType, tipo_agente)):
            uri = gr.value(subject=s, predicate=DSO.Uri)
            name = gr.value(subject=s, predicate=FOAF.name)
            address = gr.value(subject=s, predicate=DSO.Address)
            
            if uri and address:
                agentes_encontrados.append({
                    'name': str(name) if name else tipo_str,
                    'uri': uri,
                    'address': address
                })
        
        # Método 2: Buscar en el contenido
        if not agentes_encontrados:
            for s, p, o in gr.triples((content, DSO.Address, None)):
                uri = gr.value(subject=s, predicate=DSO.Uri)
                name = gr.value(subject=s, predicate=FOAF.name)
                
                if uri:
                    agentes_encontrados.append({
                        'name': str(name) if name else tipo_str,
                        'uri': uri,
                        'address': o
                    })
        
        if agentes_encontrados:
            logger.info(f"Encontrados {len(agentes_encontrados)} agentes de tipo {tipo_str}")
            return agentes_encontrados
        else:
            logger.warning(f"No se encontró ningún agente de tipo {tipo_str}")
            return None
            
    except Exception as e:
        logger.error(f"Error al buscar agente: {e}")
        traceback.print_exc()
        return None

def cargar_valoraciones():
    """
    Carga las valoraciones desde el archivo RDF si existe
    """
    global g_valoraciones
    try:
        if os.path.exists("databases/valoraciones.rdf"):
            g_valoraciones.parse("databases/valoraciones.rdf", format="xml")
            num_valoraciones = len(list(g_valoraciones.subjects(RDF.type, onto.Valoracion)))
            logger.info(f"Base de datos de valoraciones cargada con {num_valoraciones} valoraciones")
        else:
            logger.info("No existe archivo de valoraciones previo, se creará al guardar la primera valoración")
        return True
    except Exception as e:
        logger.error(f"Error al cargar valoraciones: {e}")
        return False
        

@app.route("/test_recomendar_usuario")
def test_recomendar_usuario():
    """
    Endpoint para crear un usuario de prueba con perfil específico
    y generar una recomendación para él
    """
    # Definir perfil para usuario de prueba
    perfil_deseado = request.args.get('perfil', 'Culturales')
    if perfil_deseado not in ['Culturales', 'Aventureros', 'Gastronómicos', 'Naturalistas']:
        perfil_deseado = 'Culturales'
    
    # Crear usuario de prueba con ID único
    usuario_id = f"usuario_prueba_{uuid.uuid4()}"
    usuario_uri = f"http://www.semanticweb.org/usuario/{usuario_id}"
    
    # Asignar directamente al perfil deseado
    usuarios_perfiles[usuario_uri] = perfil_deseado
    perfiles_colaborativos[perfil_deseado].añadir_usuario(usuario_uri)
    
    # Simular algunas valoraciones para reforzar el perfil
    # Esto ayuda a construir preferencias realistas
    simular_valoraciones_usuario(usuario_uri, perfil_deseado)
    
    # Ejecutar recomendación específica para este usuario
    result = recomendar_actividad_usuario(usuario_uri)
    
    return f'''
    <html>
        <head>
            <title>Recomendación de Actividad</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .info {{ background: #f0f0f0; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
                .recomendacion {{ background: #d4edda; color: #155724; padding: 15px; border-radius: 5px; }}
                pre {{ background: #f8f9fa; padding: 10px; border-radius: 3px; overflow-x: auto; }}
            </style>
        </head>
        <body>
            <h1>Test de Recomendación de Actividades</h1>
            
            <div class="info">
                <h2>Usuario de prueba</h2>
                <p><strong>URI:</strong> {usuario_uri}</p>
                <p><strong>Perfil asignado:</strong> {perfil_deseado}</p>
            </div>
            
            <div class="recomendacion">
                <h2>Recomendación generada</h2>
                <pre>{result}</pre>
            </div>
            
            <p><a href="/test">Volver al panel de pruebas</a></p>
        </body>
    </html>
    '''

def simular_valoraciones_usuario(usuario_uri, perfil_deseado):
    """
    Simula valoraciones para un usuario según el perfil deseado
    """
    # Mapeo de perfiles a tipos de actividades que preferirían
    preferencias_por_perfil = {
        'Culturales': ['Cultural'],
        'Aventureros': ['Aventura', 'Exterior'],
        'Gastronómicos': ['Gastronomica'],
        'Naturalistas': ['Naturaleza']
    }
    
    # Tipos preferidos para este perfil
    tipos_preferidos = preferencias_por_perfil.get(perfil_deseado, ['Cultural'])
    
    # Cargar planes para tener actividades reales
    cargar_planes()
    
    # Buscar actividades del tipo preferido
    actividades_valoradas = 0
    for tipo in tipos_preferidos:
        tipo_uri = getattr(onto, tipo, None)
        if not tipo_uri:
            continue
            
        for actividad in g_planes.subjects(RDF.type, tipo_uri):
            # Crear un plan ficticio para esta actividad
            plan_uri = URIRef(f"http://www.semanticweb.org/ontologia/plan_simulado/{uuid.uuid4()}")
            g_valoraciones.add((plan_uri, RDF.type, onto.Plan))
            g_valoraciones.add((plan_uri, onto.seRealizan, actividad))
            
            # Crear valoración positiva (4-5)
            valoracion_uri = URIRef(f"http://www.semanticweb.org/ontologia/valoracion_simulada/{uuid.uuid4()}")
            puntuacion = random.randint(4, 5)  # Valoración alta para reforzar perfil
            g_valoraciones.add((valoracion_uri, RDF.type, onto.Valoracion))
            g_valoraciones.add((valoracion_uri, onto.deUsuario, URIRef(usuario_uri)))
            g_valoraciones.add((valoracion_uri, onto.sobrePlan, plan_uri))
            g_valoraciones.add((valoracion_uri, onto.puntuacion, Literal(puntuacion, datatype=XSD.integer)))
            g_valoraciones.add((valoracion_uri, RDFS.comment, Literal("Valoración simulada")))
            
            # Solo crear algunas valoraciones (2-3 por tipo)
            actividades_valoradas += 1
            if actividades_valoradas >= 3:
                break
    
    # También agregar alguna ciudad visitada al perfil
    if perfil_deseado == 'Culturales':
        ciudades = ['Barcelona', 'Madrid', 'Roma']
    elif perfil_deseado == 'Aventureros':
        ciudades = ['Queenstown', 'Interlaken', 'Chamonix']
    elif perfil_deseado == 'Gastronómicos':
        ciudades = ['Lyon', 'San Sebastián', 'Tokio']
    else:  # Naturalistas
        ciudades = ['Costa Rica', 'Yellowstone', 'Islandia']
    
    # Registrar ciudades en el perfil
    for ciudad in ciudades:
        perfiles_colaborativos[perfil_deseado].registrar_ciudad(ciudad)
    
    logger.info(f"Simuladas {actividades_valoradas} valoraciones para usuario {usuario_uri} en perfil {perfil_deseado}")
    return actividades_valoradas

def recomendar_actividad_usuario(usuario_uri):
    """
    Genera una recomendación específica para un usuario y la muestra por terminal
    """
    logger.info(f"Generando recomendación personalizada para usuario: {usuario_uri}")
    
    # Cargar planes y actividades
    cargar_planes()
    
    try:
        # Obtener perfil del usuario
        perfil = obtener_perfil_usuario(usuario_uri)
        logger.info(f"RECOMENDADOR: Usuario {usuario_uri} pertenece al perfil {perfil.nombre}")
        
        # Determinar tipo de actividad preferido
        tipo_preferido = perfil.calcular_prioridad_actividad()
        if not tipo_preferido:
            logger.info("RECOMENDADOR: No se pudo determinar tipo de actividad preferido, usando Cultural")
            tipo_preferido = 'Cultural'
        
        logger.info(f"RECOMENDADOR: Tipo de actividad preferida: {tipo_preferido}")
        
        # Recuperar actividades ya realizadas
        actividades_realizadas = set()
        for val in g_valoraciones.subjects(RDF.type, onto.Valoracion):
            if (val, onto.deUsuario, URIRef(usuario_uri)) in g_valoraciones:
                plan = g_valoraciones.value(subject=val, predicate=onto.sobrePlan)
                if plan:
                    for act in g_planes.objects(subject=plan, predicate=onto.seRealizan):
                        actividades_realizadas.add(str(act))
        
        logger.info(f"RECOMENDADOR: Usuario ha realizado {len(actividades_realizadas)} actividades anteriormente")
        
        # Buscar actividades recomendables
        tipo_uri = getattr(onto, tipo_preferido)
        actividades_recomendables = []
        
        for actividad in g_planes.subjects(RDF.type, tipo_uri):
            # Filtrar actividades ya realizadas
            if str(actividad) not in actividades_realizadas:
                nombre = g_planes.value(subject=actividad, predicate=RDFS.label) or "Actividad sin nombre"
                
                # Obtener ubicación
                ciudad = "Desconocida"
                for s, p, o in g_planes.triples((actividad, onto.sehaceEn, None)):
                    ciudad_nombre = g_planes.value(subject=o, predicate=onto.NombreCiudad)
                    if ciudad_nombre:
                        ciudad = str(ciudad_nombre)
                        break
                
                actividades_recomendables.append({
                    'uri': actividad,
                    'nombre': str(nombre),
                    'tipo': tipo_preferido,
                    'ciudad': ciudad
                })
        
        logger.info(f"RECOMENDADOR: Se encontraron {len(actividades_recomendables)} actividades recomendables")
        
        # Si hay actividades recomendables, elegir una
        resultado = ""
        if actividades_recomendables:
            # Seleccionar la más adecuada (en este caso, una aleatoria)
            recomendada = random.choice(actividades_recomendables)
            
            mensaje = f"""
=============================================================
RECOMENDACIÓN DE ACTIVIDAD GENERADA
=============================================================
Usuario: {usuario_uri}
Perfil: {perfil.nombre}
Tipo actividad preferida: {tipo_preferido}
-------------------------------------------------------------
ACTIVIDAD RECOMENDADA: {recomendada['nombre']}
Ubicación: {recomendada['ciudad']}
Tipo: {recomendada['tipo']}
URI: {recomendada['uri']}
=============================================================
"""
            # Mostrar en consola y preparar para web
            print(mensaje)
            resultado = mensaje
            logger.info(f"RECOMENDADOR: Recomendación generada: {recomendada['nombre']} en {recomendada['ciudad']}")
        else:
            mensaje = f"""
=============================================================
NO SE PUDO GENERAR RECOMENDACIÓN
=============================================================
Usuario: {usuario_uri}
Perfil: {perfil.nombre}
Tipo actividad preferida: {tipo_preferido}
Motivo: No se encontraron actividades adecuadas
=============================================================
"""
            print(mensaje)
            resultado = mensaje
            logger.info("RECOMENDADOR: No se pudieron encontrar actividades recomendables")
        
        return resultado
        
    except Exception as e:
        error = f"Error al generar recomendación: {e}"
        logger.error(error)
        traceback.print_exc()
        return error


if __name__ == '__main__':
    try:
        # Registrar el agente en el directorio
        gmess = Graph()
        gmess.bind('foaf', FOAF)
        gmess.bind('dso', DSO)
        reg_obj = agn[AgenteValoraciones.name + '-Register']
        gmess.add((reg_obj, RDF.type, DSO.Register))
        gmess.add((reg_obj, DSO.Uri, AgenteValoraciones.uri))
        gmess.add((reg_obj, FOAF.name, Literal(AgenteValoraciones.name)))
        gmess.add((reg_obj, DSO.Address, Literal(AgenteValoraciones.address)))
        gmess.add((reg_obj, DSO.AgentType, DSO.ValoracionAgent))        
        
        # Lo metemos en el registro de servicios
        try:
            send_message(
                build_message(gmess, ACL.request,
                            sender=AgenteValoraciones.uri,
                            receiver=DirectoryAgent.uri,
                            content=reg_obj,
                            msgcnt=mss_cnt),
                DirectoryAgent.address
            )
            mss_cnt += 1
            logger.info("Agente registrado correctamente en el directorio")
        except Exception as e:
            logger.warning(f"No se pudo conectar con el DirectoryAgent: {e}")
            logger.warning("El agente continuará funcionando sin registro en el directorio")
        
        # Cargar valoraciones previas
        cargar_valoraciones()
        
        # Colas para comunicación entre procesos
        cola1 = Queue()  # Cola para el comportamiento de valoraciones
        cola2 = Queue()  # Cola para el comportamiento de recomendaciones
        
        # Poner en marcha los behaviors
        ab2 = Process(target=agentbehavior2, args=(cola2,))
        ab2.start()
        
        # Iniciar el servidor Flask
        logger.info(f"Iniciando servidor en {hostname}:{port}")
        app.run(host=hostname, port=port)
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        traceback.print_exc()

