"""
.. module:: airtravelbookingontology

 Translated by owl2rdflib

 Translated to RDFlib from ontology http://users.ecs.soton.ac.uk/cd8e10/ontology/

 :Date 09/05/2024 06:57:50
"""
from rdflib import URIRef
from rdflib.namespace import ClosedNamespace

AIRTRAVELBOOKINGONTOLOGY =  ClosedNamespace(
    uri=URIRef('http://users.ecs.soton.ac.uk/cd8e10/ontology/'),
    terms=[
        # Classes
        'AA1514',
        'AA6138',
        'AirBooking',
        'AirbusAircraft',
        'Aircraft',
        'Airline',
        'AirlineDirectFlightBetweenLHRAndJFK',
        'AirlineFromOrToSouthamptonInternational',
        'AirlineOperateA380-800',
        'AirlinesFlight',
        'Airport',
        'AirportServedByA380-800',
        'AmericanAirlinesFlight',
        'BA0003',
        'BA0003_1',
        'BA0003_2',
        'BA0117',
        'BE880',
        'BoeingAircraft',
        'BritishAirwaysFlight',
        'BusinessClass',
        'BusinessClassReservationPassenger',
        'BusinessClassSeat',
        'BusinessReservation',
        'Class',
        'CodeShareFlight',
        'CodesharingFlight',
        'Company',
        'ConnectingFlight',
        'Country',
        'DirectFlight',
        'DomainConcept',
        'EK003',
        'EconomyClass',
        'EconomyClassReservationPassenger',
        'EconomyClassSeat',
        'EconomyReservation',
        'EmiratesFlight',
        'FirstClass',
        'FirstClassReservation',
        'FirstClassReservationPassenger',
        'FirstClassSeat',
        'Flight',
        'FlightSegment',
        'FlybeFlight',
        'GF6713',
        'GulfAirFlight',
        'HavillandCanadaAircraft',
        'ICAOCode',
        'LX22',
        'LX359',
        'LX359ConnectLX22',
        'Manufacturer',
        'NamedFlight',
        'NonStopFlight',
        'OperatingFlight',
        'Passenger',
        'PassengerHaveFirstReservationBA0117_20110401',
        'PremiumEconomyClass',
        'PremiumEconomyClassReservationPassenger',
        'PremiumEconomyClassSeat',
        'PremiumEconomyReservation',
        'QF4795',
        'QantasAirwaysFlight',
        'Reservation',
        'Seat',
        'SwissInternationalAirlinesFlight',
        'ValuePartition',

        # Object properties
        'hasClass',
        'hasCodeshareFlight',
        'hasCountry',
        'hasDestination',
        'hasICAOCode',
        'hasNextSegment',
        'hasPreviousSegment',
        'hasReservation',
        'hasSameSegment',
        'hasSeat',
        'hasSegment',
        'isArrivedAt',
        'isCodesharedBy',
        'isCodesharing',
        'isConnectedAt',
        'isCountryOf',
        'isDepaturedFrom',
        'isDestinationOf',
        'isEquipmentedBy',
        'isEquipmenting',
        'isICAOCodeOf',
        'isManufacturedBy',
        'isManufacturerOf',
        'isOperatedBy',
        'isOperatorOf',
        'isPartSegmentOf',
        'isReservatedBy',
        'isReservating',
        'isReservationOf',
        'isSeatOf',

        # Data properties
        'hasSeatNumber',
        'isDeparturedOn'

        # Named Individuals
    ]
)
