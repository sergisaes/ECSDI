# -*- coding: utf-8 -*-
"""
File: infoLinkedGeodata.py

Created on 01/02/2014 11:32

Ejemplo de queries SPARQL en Linked Geodata

@author: bejar

Requiere mirarse la ontologia LGDO y los atributos/relaciones que
tienen definidos

lgdo:Amenity
  lgdo:Bar
  lgdo:Restaurant
  lgdo:Attraction
  lgdo:TourismThing
  lgdo:Cafe
  lgdo:FastFood
  lgdo:Pub
  lgdo:Attraction
  lgdo:PointOfInterest
  lgdo:Hotel

"""

from SPARQLWrapper import SPARQLWrapper, JSON
from AgentUtil.SPARQLPoints import GEODATA, LGEODATA

__author__ = 'javier'

sparql = SPARQLWrapper(GEODATA)

# Restaurantes que estan alrededor de 400m de long 2.16, lat 41.4
sparql.setQuery("""
  Prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#>
  Prefix ogc: <http://www.opengis.net/ont/geosparql#>
  Prefix geom: <http://geovocab.org/geometry#>
  Prefix lgdo: <http://linkedgeodata.org/ontology/>

  Select *
  WHERE {
      ?s  rdf:type lgdo:Restaurant ;
      rdfs:label ?l ;
      geom:geometry [ogc:asWKT ?g] .
      Filter(bif:st_intersects (?g, bif:st_point (2.16, 41.4), 0.4)) .
  }
  """)
sparql.setReturnFormat(JSON)
results = sparql.query().convert()
vars = results['head']['vars']
for res in results['results']['bindings']:
    for v in vars:
        print(v, ':', res[v])



