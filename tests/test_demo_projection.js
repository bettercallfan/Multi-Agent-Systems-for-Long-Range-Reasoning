const assert = require("node:assert/strict");
const fs = require("node:fs");
const {project, eventsFor} = require("../dashboard/demo-model.js");
const state = JSON.parse(fs.readFileSync("outputs/runs/20260910_163108/run_state.json","utf8"));
const events = eventsFor(state);
assert.equal(project(state,0).finished,false);
assert.equal(Object.keys(project(state,0).nodes).length,0);
for (const type of ["node_failure","requirement_change","data_anomaly"]) {
  const index = events.findIndex(e=>e.type==="runtime_injection_triggered" && e.payload.type===type);
  const before = project(state,index-1), at = project(state,index);
  assert(!Object.values(before.injections).some(i=>i.type===type && i.phase==="recovered"));
  assert(Object.values(at.injections).some(i=>i.type===type && i.phase==="triggered"));
  assert.equal(at.finished,false);
}
const final = project(state,events.length-1);
assert.equal(final.finished,true);
assert.equal(Object.values(final.injections).filter(i=>i.phase==="recovered").length,3);
assert.equal(Object.values(final.nodes).filter(n=>n.status==="failed").length,0);
console.log("Replay projection: chronology, injection lifecycle and no premature PASS verified.");
