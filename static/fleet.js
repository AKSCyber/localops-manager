function fleetMetricText(item) {
  const metrics = item.metrics || {};
  const cpu = Number.isFinite(metrics.cpu?.percent) ? `${metrics.cpu.percent.toFixed(0)}% CPU` : 'CPU unavailable';
  const ram = Number.isFinite(metrics.memory?.percent) ? `${metrics.memory.percent.toFixed(0)}% RAM` : 'RAM unavailable';
  return `${cpu} · ${ram}`;
}
let fleetNodes = [], fleetLoading = false, fleetActionBusy = false, fleetDialogMode = 'pair';

async function refreshFleet() {
  if (fleetLoading || !state.auth || $('#appShell').hidden) return;
  fleetLoading = true;
  try {
    const data = await api('/api/fleet');
    fleetNodes = data.nodes;
    $('#fleetNotice').textContent = `${data.nodes.filter(n => n.online).length} of ${data.nodes.length} computers connected · Updated ${formatTime(data.observedAt)}`;
    $('#fleetGrid').innerHTML = fleetNodes.map(item => {
      const node = item.node, local = node.id === state.node?.id;
      return `<article class="panel fleet-card ${item.online ? '' : 'fleet-offline'}"><div class="panel-head"><div><h3>${escapeHtml(node.computerName)}</h3><small>${local ? 'This dashboard' : 'Paired computer'} · ${item.online ? 'Connected' : 'Unavailable'}</small></div>${local ? '' : `<button class="button secondary" data-fleet-remove="${escapeHtml(node.id)}">Disconnect</button>`}</div><p class="fleet-id">${escapeHtml(node.id)}</p>${item.online ? `<p>${escapeHtml(fleetMetricText(item))}</p><small>Observed ${formatTime(item.observedAt)}</small><div class="fleet-apps">${(item.apps || []).map(app => `<div class="fleet-app"><div><strong>${escapeHtml(app.name)}</strong><small>${escapeHtml(stateLabel(app.state))}</small></div><div class="fleet-toolbar">${['start','stop','restart'].map(action => `<button class="button secondary" data-fleet-node="${escapeHtml(node.id)}" data-fleet-app="${escapeHtml(app.id)}" data-fleet-action="${action}" ${!app.actions?.[action] || fleetActionBusy ? 'disabled' : ''}>${action[0].toUpperCase()+action.slice(1)}</button>`).join('')}</div></div>`).join('') || '<p>No enabled applications.</p>'}</div>` : `<p role="status">${escapeHtml(item.error || 'No current status available. Controls are disabled.')}</p>`}</article>`;
    }).join('');
  } catch (error) {
    // Remove stale controls when the dashboard loses its fleet connection.
    fleetNodes = [];
    $('#fleetGrid').replaceChildren();
    $('#fleetNotice').textContent = `Computer status unavailable: ${error.message}`;
  } finally { fleetLoading = false; }
}

async function refreshFleetGrants() {
  try {
    const data = await api('/api/fleet/grants');
    $('#fleetGrants').innerHTML = data.grants.map(g => `<div class="fleet-app"><div><strong>${escapeHtml(g.label)}</strong><small>Expires ${escapeHtml(new Date(g.expiresAt * 1000).toLocaleDateString())}</small></div><button class="button secondary" data-fleet-revoke="${escapeHtml(g.id)}">Revoke</button></div>`).join('') || '<p>No access keys issued.</p>';
  } catch (error) { $('#fleetGrants').textContent = error.message; }
}

function openFleetDialog(mode) {
  fleetDialogMode = mode;
  $('#fleetForm').reset();
  $('#fleetOwnToken').value = '';
  $('#fleetDialogError').textContent = '';
  $('#fleetPairFields').hidden = mode !== 'pair';
  $('#fleetGrantFields').hidden = mode !== 'grant';
  $('#fleetGrantResult').hidden = true;
  $('#fleetDialogSubmit').hidden = false;
  $('#fleetDialogSubmit').textContent = mode === 'pair' ? 'Connect' : 'Create access key';
  $('#fleetDialogTitle').textContent = mode === 'pair' ? 'Connect a computer' : 'Share this computer';
  $('#fleetDialogHelp').textContent = mode === 'pair' ? 'On the other computer, open Share this computer, then paste its machine ID and access key here. The key is stored encrypted by Windows on this dashboard server.' : 'Allow another dashboard to read this computer’s status and start, stop or restart its registered apps. You can revoke the key below at any time.';
  $('#fleetDialog').showModal();
}

$('#fleetPair').addEventListener('click', () => openFleetDialog('pair'));
$('#fleetGrant').addEventListener('click', () => openFleetDialog('grant'));
$('#fleetRefresh').addEventListener('click', refreshFleet);
$('#fleetDialogClose').addEventListener('click', () => $('#fleetDialog').close());
$('#fleetDialog').addEventListener('close', () => { $('#fleetOwnToken').value = ''; $('#fleetForm').elements.token.value = ''; });
$('#fleetForm').addEventListener('submit', async event => {
  event.preventDefault();
  const fields = event.target.elements, button = $('#fleetDialogSubmit');
  button.disabled = true;
  $('#fleetDialogError').textContent = '';
  try {
    if (fleetDialogMode === 'pair') {
      await api('/api/fleet/pair', {method:'POST', body:JSON.stringify({id:fields.nodeId.value.trim(), url:fields.url.value.trim(), token:fields.token.value.trim()})});
      $('#fleetDialog').close();
      await refreshFleet();
      toast('Computer connected', 'Its status and application controls are now shown here.');
    } else {
      const data = await api('/api/fleet/grant', {method:'POST', body:JSON.stringify({label:fields.label.value})});
      $('#fleetGrantFields').hidden = true;
      $('#fleetGrantResult').hidden = false;
      $('#fleetOwnId').value = data.node.id;
      $('#fleetOwnToken').value = data.token;
      button.hidden = true;
      await refreshFleetGrants();
    }
  } catch (error) { $('#fleetDialogError').textContent = error.message; }
  finally { button.disabled = false; }
});

$('#fleetGrid').addEventListener('click', async event => {
  const remove = event.target.closest('[data-fleet-remove]');
  if (remove) {
    const node = fleetNodes.find(n => n.node.id === remove.dataset.fleetRemove)?.node;
    if (!node || !confirm(`Disconnect ${node.computerName} from this dashboard? To revoke its access key, use that computer’s access list.`)) return;
    try { await api('/api/fleet/remove', {method:'POST', body:JSON.stringify({nodeId:node.id})}); await refreshFleet(); }
    catch (error) { toast('Could not disconnect', error.message, true); }
    return;
  }
  const button = event.target.closest('[data-fleet-action]');
  if (!button || fleetActionBusy) return;
  const item = fleetNodes.find(n => n.node.id === button.dataset.fleetNode);
  const app = item?.apps?.find(a => a.id === button.dataset.fleetApp);
  const action = button.dataset.fleetAction;
  if (!item?.online || !app?.actions?.[action]) return;
  if (!confirm(`${action.toUpperCase()} ${app.name} on ${item.node.computerName}?\nMachine ID: ${item.node.id}`)) return;
  fleetActionBusy = true;
  $$('#fleetGrid [data-fleet-action]').forEach(b => { b.disabled = true; });
  try {
    await api('/api/fleet/action', {method:'POST', body:JSON.stringify({nodeId:item.node.id, appId:app.id, action, confirmed:true})});
    toast('Action completed', `${app.name} · ${item.node.computerName}`);
  } catch (error) { toast('Action was not confirmed', `${error.message} Inspect the target before retrying.`, true); }
  finally { fleetActionBusy = false; await refreshFleet(); }
});

$('#fleetGrants').addEventListener('click', async event => {
  const button = event.target.closest('[data-fleet-revoke]');
  if (!button || !confirm('Revoke this access key? Dashboards using it will lose access to this computer.')) return;
  try { await api('/api/fleet/revoke', {method:'POST', body:JSON.stringify({id:button.dataset.fleetRevoke})}); await refreshFleetGrants(); }
  catch (error) { toast('Could not revoke access', error.message, true); }
});
$('.fleet-access').addEventListener('toggle', event => { if (event.target.open) refreshFleetGrants(); });
