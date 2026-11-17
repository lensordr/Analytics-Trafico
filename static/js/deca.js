'use strict';
// Documento de Control (DeCA) dialog.
// ---------------------------------------------------------------- DeCA (Documento de Control)
const DEFAULT_LOAD_KG=24000; // standard full trailer load, prefilled on new orders
const formatKg=kg=>`${Number(kg).toLocaleString('es-ES',{maximumFractionDigits:2})} kg`;
function openDeca(id){const o=state.orders.find(x=>x.id===Number(id));if(!o)return;const stops=orderStops(o),loads=stops.filter(s=>s.kind==='Loading');
  const places=(state.locations||[]).map(l=>({id:l.id,label:l.company,hint:[l.city,l.nif?'NIF '+l.nif:'no NIF'].filter(Boolean).join(' · ')}));
  const checks=[[!!(state.settings?.carrier_name&&state.settings?.carrier_nif),'Your company name and NIF','Settings & backup']];
  // Material and weight of each loading are filled in here: the order form only edits planned orders.
  const loadFields=loads.map(s=>`<div class="span-2 deca-load"><h4>Loading ${s.seq} · ${esc(getLocation(s.location_id)?.company||'?')}</h4><div class="form-grid">${materialPicker('Material loaded','deca_mat_'+s.seq,s.material_id)}${field('Weight loaded (kg)','deca_kg_'+s.seq,s.weight_kg??'','number','min="1" max="60000" step="1" required')}</div></div>`).join('');
  showModal('DeCA · '+o.number,`<form id="deca-form">${errorBox}<input type="hidden" name="order_id" value="${o.id}"><p class="subtitle">Documento de Control for this transport. The identifier stays the same when you generate it again; the PDF always shows the current order data.</p><div class="form-grid">${searchSelectField('Cargador (shipper · who contracts the transport)','shipper_id',places,o.deca_shipper_id||o.loading_id,{required:true,placeholder:'Search company…',noResults:'No match. Add the company in Saved locations.'})}${loadFields}<div class="field span-2"><label for="f-deca-notes">Observaciones (optional)</label><textarea id="f-deca-notes" name="notes" maxlength="500">${esc(o.deca_notes||'')}</textarea></div></div>
  <ul class="deca-checks">${checks.map(([ok,label,where])=>`<li class="${ok?'ok':'missing'}">${ok?'✓':'✗'} ${esc(label)}${ok?'':` <small>→ ${esc(where)}</small>`}</li>`).join('')}<li class="info">The cargador must have a NIF in Saved locations.</li></ul>
  <div id="deca-result"></div><div class="form-actions"><button type="button" class="btn" data-action="close">Close</button><button class="btn primary" type="submit">${o.deca_id?'Update & open PDF':'Generate DeCA'}</button></div></form>`,'DOCUMENTO DE CONTROL');}
