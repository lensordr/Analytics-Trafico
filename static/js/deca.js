'use strict';
// Documento de Control (DeCA) dialog.
// ---------------------------------------------------------------- DeCA (Documento de Control)
const DEFAULT_LOAD_KG=24000; // standard full trailer load, prefilled on new orders
const formatKg=kg=>`${Number(kg).toLocaleString('es-ES',{maximumFractionDigits:2})} kg`;
function openDeca(id){const o=state.orders.find(x=>x.id===Number(id));if(!o)return;const stops=orderStops(o),loads=stops.filter(s=>s.kind==='Loading');
  const places=(state.locations||[]).map(l=>({id:l.id,label:l.company,hint:[l.city,l.nif?'NIF '+l.nif:'no NIF'].filter(Boolean).join(' · ')}));
  const checks=[[!!(state.settings?.carrier_name&&state.settings?.carrier_nif),'Your company name and NIF','Settings & backup'],...loads.map(s=>[!!(s.material&&s.weight_kg),`Stop ${s.seq} · ${getLocation(s.location_id)?.company||'?'}: material and weight`,'Edit the order'])];
  showModal('DeCA · '+o.number,`<form id="deca-form">${errorBox}<input type="hidden" name="order_id" value="${o.id}"><p class="subtitle">Documento de Control for this transport. The identifier stays the same when you generate it again; the PDF always shows the current order data.</p><div class="form-grid">${searchSelectField('Cargador (shipper · who contracts the transport)','shipper_id',places,o.deca_shipper_id||o.loading_id,{required:true,placeholder:'Search company…',noResults:'No match. Add the company in Saved locations.'})}<div class="field span-2"><label for="f-deca-notes">Observaciones (optional)</label><textarea id="f-deca-notes" name="notes" maxlength="500">${esc(o.deca_notes||'')}</textarea></div></div>
  <ul class="deca-checks">${checks.map(([ok,label,where])=>`<li class="${ok?'ok':'missing'}">${ok?'✓':'✗'} ${esc(label)}${ok?'':` <small>→ ${esc(where)}</small>`}</li>`).join('')}<li class="info">The cargador must have a NIF in Saved locations.</li></ul>
  <div id="deca-result"></div><div class="form-actions"><button type="button" class="btn" data-action="close">Close</button><button class="btn primary" type="submit">${o.deca_id?'Update & open PDF':'Generate DeCA'}</button></div></form>`,'DOCUMENTO DE CONTROL');}
