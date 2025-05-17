#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Script para probar el Agente del Clima mediante peticiones programáticas
"""

import argparse
import requests
import uuid
from rdflib import Graph, Namespace, URIRef, Literal
from rdflib.namespace import RDF

# Configuración
parser = argparse.ArgumentParser(description="Prueba del Agente del Clima")
parser.add_argument('-c', '--ciudad', type=str, default="Barcelona", help="Nombre de la ciudad")
parser.add_argument('-p', '--pais', type=str, default="España", help="Nombre del país")
parser.add_argument('-d', '--dias', type=int, default=3, help="Días de previsión (1-5)")
parser.add_argument('-u', '--url', type=str, default="http://localhost:9002/comm", help="URL del agente clima")

args = parser.parse_args()

# Espacios de nombres
onto = Namespace("http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2/")

# Crear la petición
g = Graph()
g.bind('rdf', RDF)
g.bind('onto', onto)

# Crear objeto de petición
peticion_id = URIRef(f'peticion_clima_{uuid.uuid4()}')
g.add((peticion_id, RDF.type, onto.PeticionClima))

# Crear y añadir ciudad
ciudad_id = URIRef(f'ciudad_{uuid.uuid4()}')
g.add((ciudad_id, onto.NombreCiudad, Literal(args.ciudad)))
g.add((ciudad_id, onto.NombrePais, Literal(args.pais)))
g.add((peticion_id, onto.comoRestriccionLocalidad, ciudad_id))

# Añadir días
g.add((peticion_id, onto.duranteUnTiempo, Literal(args.dias)))

# Enviar petición al agente
print(f"Enviando petición a {args.url}")
print(f"Ciudad: {args.ciudad}, País: {args.pais}, Días: {args.dias}")

try:
    # Serializar el grafo a XML
    content = g.serialize(format='xml')
    
    # Enviar la petición
    resp = requests.get(args.url, params={'content': content})
    
    # Mostrar resultados
    print(f"Estado de la respuesta: {resp.status_code}")
    if resp.status_code == 200:
        print("Respuesta recibida con éxito.")
        # Procesar el grafo de respuesta
        resp_g = Graph()
        resp_g.parse(data=resp.text, format='xml')
        print("\nDatos de la respuesta:")
        
        # Buscar información climática
        for s, p, o in resp_g.triples((None, RDF.type, onto.RespuestaClima)):
            print(f"\nRespuesta ID: {s}")
            
            # Buscar información actual
            for s1, p1, o1 in resp_g.triples((s, onto.climaActual, None)):
                print("\nClima Actual:")
                for s2, p2, o2 in resp_g.triples((o1, None, None)):
                    if p2 != RDF.type:
                        print(f"  {p2.split('/')[-1]}: {o2}")
            
            # Buscar previsiones
            print("\nPrevisiones:")
            for s1, p1, o1 in resp_g.triples((s, onto.previsiones, None)):
                print("\n - Día:")
                for s2, p2, o2 in resp_g.triples((o1, None, None)):
                    if p2 != RDF.type:
                        print(f"   {p2.split('/')[-1]}: {o2}")
    else:
        print(f"Error: {resp.text}")
except Exception as e:
    print(f"Error al procesar la petición: {e}")