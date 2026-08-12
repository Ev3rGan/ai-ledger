# Use PostgreSQL for hybrid knowledge retrieval

PostgreSQL is the system of record for publications, source definitions, documents, stories, claims, evidence, editorial state, and traces, while pgvector and PostgreSQL full-text search provide semantic and lexical retrieval. The project will not add a separate vector database or graph database for the MVP; relational provenance, transactional consistency, operational simplicity, and the infrastructure budget outweigh the possible specialization of additional stores.
