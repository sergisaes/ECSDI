"""
.. module:: ontologiaviajes

 Translated by owl2rdflib

 Translated to RDFlib from ontology http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2

 :Date 03/06/2025 00:59:43
"""
from rdflib import URIRef
from rdflib.namespace import ClosedNamespace
from SPARQLWrapper import SPARQLWrapper, JSON, POST

ONTOLOGIAVIAJES =  ClosedNamespace(
    uri=URIRef('http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2'),
    terms=[
        # Classes
        'IntervaloPrecio',
        'Lluvioso',
        'Nevado',
        'Nublado',
        'PeticionMeteorologica',
        'PeticionReplanificacion',
        'PeticionValoracion',
        'Recomendacion',
        'Respuesta',
        'RespuestaActividad',
        'RespuestaAlojamiento',
        'RespuestaAlternativa',
        'RespuestaMeteorologica',
        'RespuestaPagoContrato',
        'RespuestaPagoJustificacion',
        'RespuestaPagoRecibido',
        'RespuestaPlan',
        'RespuestaRecomendacion',
        'RespuestaTransporte',
        'Soleado',
        'TiempoMeteorologico',
        'Tormentoso',
        'Actividad',
        'Alojamiento',
        'Aventura',
        'Avion',
        'Ciudad',
        'Cultural',
        'Exterior',
        'Gastronomica',
        'Interior',
        'Lugar',
        'Naturaleza',
        'Pais',
        'Peticion',
        'PeticionActividad',
        'PeticionAlojamiento',
        'PeticionPago',
        'PeticionPagoPorContrato',
        'PeticionPagoPorPasarela',
        'PeticionPlan',
        'PeticionTransporte',
        'Plan',
        'PlanDe1Dia',
        'PlanGeneral',
        'Rango',
        'Transporte',
        'Tren',
        'Usuario',
        'Valoracion',

        # Object properties
        'cambiaPor',
        'comoDestino',
        'comoOrigen',
        'comoPedidor',
        'comoRestriccionLocalidad',
        'formadoPorActividades',
        'formadoPorAlojamientos',
        'formadoPorPlan',
        'formadoPorTransportes',
        'porMotivoDe',
        'sustituyeA',
        'tieneComoPlan',
        'tiene_como_precio',
        'alojaminetoEn',
        'duranteUnTiempo',
        'esRealizadoPor',
        'estaCompuestoPor',
        'estaEn',
        'hasTransport',
        'llegaA',
        'saleDe',
        'seRealizan',
        'sehaceEn',
        'tieneAlojamiento',
        'tieneValoracion',
        'transporteVuelta',

        # Data properties
        'ActividadCancelada',
        'TemporalPerjudicial',
        'importe',
        'CuentaBancaria',
        'Exterior',
        'IdPlan',
        'IdTren',
        'IdVuelo',
        'ImportePago',
        'Llegada',
        'NombreCiudad',
        'NombrePais',
        'NombreUsuario',
        'Precio',
        'PrecioMax',
        'PrecioMin',
        'Puntuacion',
        'RadioAlojamiento',
        'Salida',
        'Ubicacion'

        # Named Individuals
    ]
)

# Configure the base URL for the Fuseki server
# Use a variable instead of hardcoding "localhost" so it can be changed
FUSEKI_SERVER = "192.168.1.43"
FUSEKI_PORT = 3030
FUSEKI_BASE_URL = f"http://{FUSEKI_SERVER}:{FUSEKI_PORT}"

# Function to get SPARQL endpoints
def get_query_endpoint(dataset):
    return f"{FUSEKI_BASE_URL}/{dataset}/sparql"

def get_update_endpoint(dataset):
    return f"{FUSEKI_BASE_URL}/{dataset}/update"

def query_sparql(dataset, query_string):
    sparql = SPARQLWrapper(get_query_endpoint(dataset))
    sparql.setQuery(query_string)
    sparql.setReturnFormat(JSON)
    results = sparql.query().convert()
    return results

def update_sparql(dataset, update_string):
    sparql = SPARQLWrapper(get_update_endpoint(dataset))
    sparql.setMethod(POST)
    sparql.setQuery(update_string)
    sparql.query()

def test_fuseki_connection():
    """Test connection to Fuseki"""
    import requests
    try:
        response = requests.get(FUSEKI_BASE_URL)
        print(f"Connected to Fuseki! Status: {response.status_code}")
        return True
    except Exception as e:
        print(f"Failed to connect to Fuseki: {e}")
        return False
