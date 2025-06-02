"""
.. module:: ontologiaviajes

 Translated by owl2rdflib

 Translated to RDFlib from ontology http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2

 :Date 03/06/2025 00:59:43
"""
from rdflib import URIRef
from rdflib.namespace import ClosedNamespace

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
