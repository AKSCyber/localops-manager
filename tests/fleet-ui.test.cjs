const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {hidden:false, textContent:'', innerHTML:'', value:'', listeners:{},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    replaceChildren() { this.innerHTML = ''; }});
  return elements.get(id);
}
const calls = [];
const nodes = [
  {node:{id:'maximus',computerName:'Maximus'},online:true,observedAt:'now',metrics:{cpu:{percent:25},memory:{percent:40}},apps:[{id:'studio',name:'Studio',state:'running',actions:{stop:true}}]},
  {node:{id:'helios',computerName:'Helios'},online:true,observedAt:'now',apps:[{id:'studio',name:'Studio',state:'running',actions:{stop:true}}]},
  {node:{id:'offline',computerName:'<Offline>'},online:false,error:'Unreachable',apps:[]}
];
let fail = false;
const context = vm.createContext({
  console, Number, Date, JSON, state:{auth:{},node:{id:'maximus'}},
  $:element, $$:()=>[], escapeHtml:s=>String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
  stateLabel:s=>s, formatTime:s=>s, confirm:()=>true, toast:()=>{},
  api:async (path, options)=>{ calls.push({path,options}); if(fail) throw Error('Disconnected'); return {nodes,observedAt:'now'}; }
});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../static/fleet.js'),'utf8'),context);
(async()=>{
  await vm.runInContext('refreshFleet()',context);
  assert.match(element('#fleetGrid').innerHTML,/Maximus/);
  assert.match(element('#fleetGrid').innerHTML,/Helios/);
  assert.match(element('#fleetGrid').innerHTML,/25% CPU/);
  assert.match(element('#fleetGrid').innerHTML,/&lt;Offline&gt;/);
  assert.doesNotMatch(element('#fleetGrid').innerHTML,/data-fleet-node="offline"/);
  const button = {dataset:{fleetNode:'helios',fleetApp:'studio',fleetAction:'stop'}};
  await element('#fleetGrid').listeners.click({target:{closest:selector=>selector==='[data-fleet-action]' ? button : null}});
  const command = calls.find(call=>call.path==='/api/fleet/action');
  assert.equal(JSON.parse(command.options.body).nodeId,'helios');
  assert.equal(JSON.parse(command.options.body).appId,'studio');
  fail = true;
  await vm.runInContext('refreshFleet()',context);
  assert.equal(element('#fleetGrid').innerHTML,'');
  assert.match(element('#fleetNotice').textContent,/unavailable/);
  console.log('Fleet UI: combined rendering, telemetry, escaping, routing and stale-control removal passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
