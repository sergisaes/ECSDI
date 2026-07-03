<h1 align="center">ECSDI — Distributed Multi-Agent Travel Planner</h1>

<p align="center">
  A distributed multi-agent system that plans trips end-to-end, with agents that negotiate over a
  shared semantic-web knowledge base (RDF/OWL ontologies + SPARQL on a Jena Fuseki triple store).
</p>

---

## What it is

Course project for **ECSDI** (Knowledge Engineering & Distributed Information Systems) at UPC. A set
of autonomous agents cooperate to build a travel plan: each owns a domain, communicates via
FIPA-ACL–style messages, and reasons over a shared ontology instead of hard-coded data.

## Agents

| Agent | Responsibility |
|-------|----------------|
| `AgentePlanes` / `AgenteMantenedorPlanes` | Orchestrates and maintains the overall trip plan |
| `AgenteAlojamientos` | Finds lodging |
| `AgenteTransportes` | Resolves transport between locations |
| `AgenteActividades` | Suggests activities |
| `AgenteClima` | Weather constraints |
| `AgentePagos` | Handles payment step |
| `AgenteValoraciones` | Ratings / feedback |
| `SimpleDirectoryService` | Agent directory / discovery |

## How it works

- **Ontology** (`OntologiaViajes` / `ontologiaviajes.py`, `.ttl`) models trips, transports, lodging,
  activities and their relationships in RDF/OWL.
- Agents publish and query knowledge via **SPARQL** against a **Jena Fuseki** triple store.
- Agents register with a directory service and exchange ACL messages to negotiate a plan.

## Run it

You need a running Jena Fuseki server (not bundled — download from the Apache Jena project) and:

```bash
pip install -r requirements.txt
python SimpleDirectoryService.py     # start the directory
python AgentePlanes.py                # start each agent (in its own terminal)
# ...
```

## Stack

Python · RDFLib · SPARQL · Apache Jena Fuseki · Flask · RDF/OWL ontologies · multi-agent / FIPA-ACL
