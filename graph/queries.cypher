// queries.cypher — Phase 5 constraints + gate/verification queries for Neo4j Browser.
// Load first with:  python src/load_graph.py           (or --wipe --yes to reset)
// Every node carries :Entity + its type label and a unique `gkey` (graph key).
// Hubs (System/Component/Cause-category/Unit/Manufacturer/RegulatoryReference/LER)
// are keyed on their stored match_key; event-specific nodes (FailureMode,
// Consequence, CorrectiveAction, provisional Cause) are keyed match_key + ':' + ler.

// ---------------------------------------------------------------------------
// 0. constraint (the loader also ensures this)
// ---------------------------------------------------------------------------
CREATE CONSTRAINT entity_gkey IF NOT EXISTS FOR (n:Entity) REQUIRE n.gkey IS UNIQUE;

// ---------------------------------------------------------------------------
// 1. sanity — counts
// ---------------------------------------------------------------------------
MATCH (n:Entity) UNWIND labels(n) AS l WITH l WHERE l <> 'Entity'
RETURN l AS label, count(*) AS n ORDER BY n DESC;

MATCH ()-[r]->() RETURN type(r) AS relationship, count(*) AS n ORDER BY n DESC;

// ---------------------------------------------------------------------------
// 2. GATE — cross-document hubs (each links two or more reports)
// ---------------------------------------------------------------------------

// 2a. every LER that occurred on the HPCI (BJ) system  -> all three plants
MATCH (l:LER)-[:INVOLVES]->(s:System {eiis_code:'BJ'})
WHERE NOT l.stub
RETURN l.key AS ler, l.plant_name AS plant ORDER BY ler;

// 2b. LERs whose loss-of-function was covered by the ADS backup  -> all three
MATCH (l:LER)-[:HAS_CAUSE]->(:Cause)<-[:CAUSED_BY]-()-[:LEADS_TO*0..]->
      (:Consequence)-[:BACKED_UP_BY]->(:System {match_key:'System:automatic-depressurization-system'})
WHERE NOT l.stub
RETURN DISTINCT l.key AS ler ORDER BY ler;

// 2c. LERs sharing the RCIC (BN) backup  -> Quad Cities + Limerick
MATCH (l:LER)-[:HAS_CAUSE]->(:Cause)<-[:CAUSED_BY]-()-[:LEADS_TO*0..]->
      (:Consequence)-[:BACKED_UP_BY]->(:System {eiis_code:'BN'})
WHERE NOT l.stub
RETURN DISTINCT l.key AS ler ORDER BY ler;

// 2d. LERs reported under the same criterion 10 CFR 50.73(a)(2)(v)(D)  -> all three
MATCH (l:LER)-[:REPORTED_UNDER]->(:RegulatoryReference {match_key:'RegulatoryReference:10-cfr-50-73-a-2-v-d'})
WHERE NOT l.stub
RETURN l.key AS ler ORDER BY ler;

// ---------------------------------------------------------------------------
// 3. GATE — a full within-report multi-hop path
//     Limerick: cannon-plug degraded -> short -> PCIV closes -> HPCI inoperable
// ---------------------------------------------------------------------------
MATCH p=(f0:FailureMode)-[:LEADS_TO*]->(c:Consequence)
WHERE f0.match_key STARTS WITH 'FailureMode:cannon-plug'
RETURN [n IN nodes(p) | n.display_name] AS chain, length(p) AS hops;

// full event picture for one LER (paste into Browser for the visual subgraph)
MATCH (l:LER {key:'353-2025-001-00'})
CALL apoc.path.subgraphAll(l, {maxLevel:6}) YIELD nodes, relationships
RETURN nodes, relationships;
// (no APOC? use:)  MATCH p=(l:LER {key:'353-2025-001-00'})-[*1..4]-(m) RETURN p;

// ---------------------------------------------------------------------------
// 4. golden-question flavor
// ---------------------------------------------------------------------------

// Q: what components / failure modes appear on HPCI across the whole corpus?
MATCH (l:LER)-[:INVOLVES]->(s:System {eiis_code:'BJ'})
OPTIONAL MATCH (l)-[:INVOLVES]->(c:Component)
RETURN l.plant_name AS plant, l.key AS ler,
       collect(DISTINCT c.display_name) AS components ORDER BY ler;

// Q: which events were mitigated by a redundant safety system being available?
MATCH (l:LER)-[:HAS_CAUSE]->(:Cause)<-[:CAUSED_BY]-()-[:LEADS_TO*0..]->
      (cons:Consequence)-[:BACKED_UP_BY]->(b:System)
WHERE NOT l.stub
RETURN l.key AS ler, cons.display_name AS consequence,
       collect(DISTINCT coalesce(b.eiis_code, b.display_name)) AS backups ORDER BY ler;

// Q: group events by normalized cause category (the cross-document join at scale)
MATCH (l:LER)-[:HAS_CAUSE]->(c:Cause)
WHERE NOT l.stub
RETURN c.category AS cause_category, c.cause_code AS code,
       collect(l.key) AS lers, count(*) AS n ORDER BY n DESC;

// ---------------------------------------------------------------------------
// 5. hygiene — orphans should be only RegulatoryReference citations
// ---------------------------------------------------------------------------
MATCH (n:Entity) WHERE NOT (n)--() AND NOT n:RegulatoryReference AND NOT n.stub
RETURN labels(n) AS labels, n.display_name AS name;
