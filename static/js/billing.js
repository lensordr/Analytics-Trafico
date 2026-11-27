'use strict';
// Billing: delivered trips waiting for an invoice (Pending), grouped by client + tractor + month, with the weight
// typed by hand and the price from the order's rate; the invoices made (Done); who issues them (Issuers).
let billingData=null,billingError='',billingTab='pending',billingMonth='',billingQuery='';
const BILL_BASIS={loading_t:'€/t loaded',unloading_t:'€/t unloaded',trip:'€/trip',hour:'€/hour'};
const billingPendingCount=()=>state?.orders?.filter(o=>o.status==='Completed'&&!o.invoice_id).length||0;
function updateBillingCount(){const el=$('#nav-billing');if(!el||!state)return;const n=billingPendingCount();el.hidden=!n;el.textContent=n;el.title=`${n} delivered trips to invoice`;}
async function loadBilling(){billingError='';try{billingData=await api('/api/billing',{});}catch(error){billingError=error.message;}if(view==='billing')render();}
const monthLabel=m=>m?new Date(m+'-15T12:00:00Z').toLocaleDateString('en-GB',{month:'long',year:'numeric',timeZone:'UTC'}):'';
const issuerOf=id=>billingData?.issuers.find(i=>i.id===id);
function billingView(){const head=heading('Billing','Delivered trips are Pending until their invoice is made, then Done. One invoice per client, tractor and month.');
  if(billingError)return head+`<div class="form-error">${esc(billingError)}</div>`;if(!billingData)return head+'<section class="panel loading">Loading billing…</section>';
  const pending=billingData.pending,trips=pending.reduce((n,g)=>n+g.lines.length,0),live=billingData.invoices.filter(i=>i.status!=='Cancelled');
  const tab=(k,label,n)=>`<button type="button" class="${billingTab===k?'selected':''}" data-bill="tab" data-tab="${k}">${label}${n!=null?` <b>${n}</b>`:''}</button>`;
  return head+`<div class="view-toggle bill-tabs">${tab('pending','⏳ Pending',trips)}${tab('done','✓ Done · invoices',live.length)}${tab('issuers','🏷 Issuers',billingData.issuers.length)}</div>`
    +(billingTab==='done'?invoicesPanel():billingTab==='issuers'?issuersPanel():pendingPanel());}
function pendingPanel(){const months=[...new Set(billingData.pending.map(g=>g.month))].sort().reverse(),q=billingQuery.trim().toLowerCase();
  const shown=billingData.pending.filter(g=>(!billingMonth||g.month===billingMonth)&&(!q||`${g.client} ${g.plate}`.toLowerCase().includes(q)));
  return `<section class="panel bill-filters"><div class="field"><label for="bill-month">Month</label><select id="bill-month" data-bill-filter="month"><option value="">All months</option>${months.map(m=>`<option value="${m}" ${m===billingMonth?'selected':''}>${monthLabel(m)}</option>`).join('')}</select></div>
  <div class="field wide"><label for="bill-q">Client or tractor</label><input id="bill-q" data-bill-filter="q" type="search" value="${esc(billingQuery)}" placeholder="Search…" autocomplete="off"></div></section>
  ${shown.map(groupCard).join('')||`<section class="panel padded"><p class="subtitle">${billingData.pending.length?'Nothing in this filter.':'No delivered trips waiting for an invoice.'}</p></section>`}`;}
function groupCard(g){const issuer=issuerOf(g.issuer_id),need=!g.client_id?'Choose the client on the orders first.':g.lines.some(l=>l.missing.length)?'Enter the weight (and the price where missing) of every trip.':'';
  return `<section class="panel bill-group" data-group="${esc(g.key)}"><div class="bill-group-head"><div><h3>${esc(g.client||'No client')} · <span class="plate">${esc(g.plate)}</span> · ${monthLabel(g.month)}</h3>
  <small>${g.lines.length} trip${g.lines.length>1?'s':''} · issued by ${esc(issuer?.name||'?')}${issuer&&!issuer.own?' (on its behalf)':''}</small></div>
  <div class="bill-group-total"><strong data-group-base>${euro(g.base)}</strong><small>before taxes</small></div>
  <button class="btn primary" data-bill="invoice" data-key="${esc(g.key)}" ${g.ready?'':'disabled'} title="${esc(need||'Make the invoice of these trips')}">Create invoice</button></div>
  ${need?`<p class="bill-need">${esc(need)}</p>`:''}
  <div class="table-wrap"><table class="bill-table"><thead><tr><th>Date</th><th>Trip</th><th>Material</th><th class="num">Km</th><th class="num">Weight kg</th><th class="num">Rate</th><th class="num">Total €</th></tr></thead>
  <tbody>${g.lines.map(billLine).join('')}</tbody></table></div></section>`;}
function billLine(l){const input=(f,value,attrs='')=>`<input data-bill-field="${f}" value="${value==null?'':esc(value)}" ${attrs}>`;
  return `<tr data-line="${l.order_id}" class="${l.missing.length?'missing':''}"><td>${formatDay(l.day)}</td>
  <td><button class="link-button" data-action="order-detail" data-id="${l.order_id}">${esc(l.number)}</button><small>${esc(l.from)} → ${esc(l.to)}</small>${l.reference?`<small>Ref. ${esc(l.reference)}</small>`:''}</td>
  <td>${input('material',l.material,'maxlength="120" class="bill-material"')}</td>
  <td class="num">${input('km',l.km,'type="number" min="0" max="15000" step="1" class="bill-num"')}</td>
  <td class="num">${input('weight_kg',l.weight_kg,`type="number" min="1" max="60000" step="1" class="bill-num ${l.weight_kg?'':'need'}" placeholder="kg" required`)}</td>
  <td class="num">${l.basis==='hour'?'<small>per hour</small>':input('rate',l.rate,'type="number" min="0" step="0.01" class="bill-num"')}<small>${esc(BILL_BASIS[l.basis]||'no rate')}</small></td>
  <td class="num">${input('amount',l.amount_manual,`type="number" min="0" step="0.01" class="bill-num ${l.amount==null?'need':''}" placeholder="${l.amount==null?'€':esc(Number(l.amount).toFixed(2))}"`)}<small>${l.amount_manual!=null?'typed':l.amount!=null?'from rate':'enter a total'}</small></td></tr>`;}
// A changed field saves the whole trip row; the group's total and button follow.
async function saveBillLine(row){const data={order_id:Number(row.dataset.line)};for(const input of row.querySelectorAll('[data-bill-field]'))data[input.dataset.billField]=input.value;
  const result=await api('/api/billing-line',data);for(const g of billingData.pending){const index=g.lines.findIndex(l=>l.order_id===data.order_id);if(index<0)continue;
    g.lines[index]=result.line;g.base=Math.round(g.lines.reduce((n,l)=>n+(l.amount||0),0)*100)/100;g.ready=!!g.client_id&&!g.lines.some(l=>l.missing.length);
    const card=row.closest('.bill-group');if(card)card.outerHTML=groupCard(g);}}
function openInvoice(key){const g=billingData.pending.find(x=>x.key===key);if(!g)return;const issuer=issuerOf(g.issuer_id),today=dayOf(state.now);
  showModal('New invoice',`<form id="invoice-form" data-key="${esc(key)}">${errorBox}
  <p class="subtitle"><strong>${esc(g.client)}</strong> · ${esc(g.plate)} · ${monthLabel(g.month)} · ${g.lines.length} trip${g.lines.length>1?'s':''}<br>Issued by <strong>${esc(issuer.name)}</strong>${issuer.own?'':` · by ${esc(billingData.issuers.find(i=>i.own)?.name||'MalcomTrans')} on its behalf`} · series ${esc(issuer.series)}</p>
  ${!issuer.nif?notice(`Add the NIF of ${esc(issuer.name)} in Billing › Issuers before making the invoice.`,true):''}
  <div class="form-grid"><div class="field"><label for="inv-date">Invoice date</label><input id="inv-date" name="issue_date" type="date" required value="${today}"></div>
  <div class="field"><label for="inv-iva">IVA %</label><input id="inv-iva" name="iva_pct" type="number" min="0" max="30" step="0.01" value="${issuer.iva_pct}"></div>
  <div class="field"><label for="inv-irpf">IRPF retention %</label><input id="inv-irpf" name="irpf_pct" type="number" min="0" max="30" step="0.01" value="${issuer.irpf_pct}"></div>
  <div class="field span-2"><label for="inv-notes">Note on the invoice (optional)</label><input id="inv-notes" name="notes" maxlength="500" placeholder="e.g. Operación no sujeta a IVA (art. 70 LIVA)"></div></div>
  <div class="bill-totals" id="inv-totals"></div>
  <div class="form-actions"><button type="button" class="btn" data-action="close">Cancel</button><button type="submit" class="btn primary">Create invoice</button></div></form>`,'BILLING');invoiceTotals();}
function invoiceTotals(){const form=$('#invoice-form');if(!form)return;const g=billingData.pending.find(x=>x.key===form.dataset.key),base=g.base,iva=Math.round(base*Number(form.elements.iva_pct.value||0))/100,irpf=Math.round(base*Number(form.elements.irpf_pct.value||0))/100;
  $('#inv-totals').innerHTML=`<div><span>Base</span><strong>${euro(base)}</strong></div><div><span>IVA</span><strong>${euro(iva)}</strong></div>${irpf?`<div><span>IRPF</span><strong>−${euro(irpf)}</strong></div>`:''}<div class="grand"><span>Total</span><strong>${euro(base+iva-irpf)}</strong></div>`;}
function invoicesPanel(){const rows=billingData.invoices;
  return `<section class="panel"><div class="table-wrap"><table class="bill-table"><thead><tr><th>Invoice</th><th>Date</th><th>Client</th><th>Tractor</th><th>Month</th><th class="num">Base</th><th class="num">IVA</th><th class="num">IRPF</th><th class="num">Total</th><th></th></tr></thead><tbody>
  ${rows.map(i=>`<tr class="${i.status==='Cancelled'?'cancelled':''}"><td><strong>${esc(i.number)}</strong>${i.status==='Cancelled'?' <span class="badge">Cancelled</span>':' <span class="badge green">Done</span>'}<small>${esc(issuerOf(i.issuer_id)?.name||'')}</small></td><td>${formatDay(i.issue_date)}</td><td>${esc(i.client)}</td><td>${esc(i.plate)}</td><td>${monthLabel(i.month)}</td>
  <td class="num">${euro(i.base)}</td><td class="num">${euro(i.iva)}</td><td class="num">${i.irpf?'−'+euro(i.irpf):'—'}</td><td class="num"><strong>${euro(i.total)}</strong></td>
  <td class="bill-actions"><a class="btn small" href="/api/invoice.pdf?id=${i.id}" target="_blank" rel="noopener">PDF</a>${i.status==='Cancelled'?'':`<button class="btn small" data-bill="cancel" data-id="${i.id}">Cancel</button>`}</td></tr>`).join('')||'<tr><td colspan="10" class="muted">No invoices yet.</td></tr>'}
  </tbody></table></div></section>`;}
function issuersPanel(){return `<section class="panel padded"><p class="subtitle">MalcomTrans issues every invoice. For the trucks of an autónomo working through MalcomTrans, the invoice is made in the autónomo's name (its NIF, series, IVA and IRPF), "en nombre y por cuenta de". Trips of carriers without their own issuer are invoiced by MalcomTrans.</p>
  <div class="actions"><button class="btn primary" data-bill="issuer">＋ Add autónomo / issuer</button></div></section>
  <section class="panel"><div class="table-wrap"><table class="bill-table"><thead><tr><th>Issuer</th><th>Carrier on the orders</th><th>NIF</th><th>Series</th><th class="num">IVA</th><th class="num">IRPF</th><th></th></tr></thead><tbody>
  ${billingData.issuers.map(i=>`<tr><td><strong>${esc(i.name)}</strong>${i.own?' <span class="badge green">Own company</span>':''}<small>${esc([i.address,i.postal_code,i.city].filter(Boolean).join(', '))}</small></td><td>${i.own?'<small>all other carriers</small>':esc(i.carrier)}</td><td>${esc(i.nif)||'<span class="bill-need">missing</span>'}</td><td>${esc(i.series)}</td><td class="num">${i.iva_pct}%</td><td class="num">${i.irpf_pct}%</td><td><button class="btn small" data-bill="issuer" data-id="${i.id}">Edit</button></td></tr>`).join('')}
  </tbody></table></div></section>`;}
function openIssuer(id){const i=billingData.issuers.find(x=>x.id===Number(id))||{country:'España',iva_pct:21,irpf_pct:1},carriers=[...new Set(state.drivers.map(d=>d.carrier).filter(c=>c&&c!=='MalcomTrans'))].sort();
  const f=(label,name,value,attrs='')=>`<div class="field"><label for="iss-${name}">${label}</label><input id="iss-${name}" name="${name}" value="${esc(value??'')}" ${attrs}></div>`;
  showModal(i.id?'Edit issuer':'New autónomo / issuer',`<form id="issuer-form">${errorBox}<input type="hidden" name="id" value="${i.id||''}"><div class="form-grid">
  ${i.own?'<input type="hidden" name="carrier" value="MalcomTrans">':`<div class="field"><label for="iss-carrier">Carrier on the orders</label><input id="iss-carrier" name="carrier" list="iss-carriers" required value="${esc(i.carrier||'')}"><datalist id="iss-carriers">${carriers.map(c=>`<option value="${esc(c)}">`).join('')}</datalist><small>As written on the drivers (Drivers & carriers).</small></div>`}
  ${f('Name on the invoice','name',i.name,'required maxlength="200"')}${f('NIF / CIF','nif',i.nif,'required maxlength="30"')}${f('Address','address',i.address,'maxlength="200"')}${f('Postal code','postal_code',i.postal_code,'maxlength="12"')}${f('City','city',i.city,'maxlength="120"')}${f('Country','country',i.country,'maxlength="60"')}${f('IBAN (for payment)','iban',i.iban,'maxlength="40"')}
  ${f('Invoice series','series',i.series,'required maxlength="12" placeholder="e.g. BA"')}${f('IVA %','iva_pct',i.iva_pct,'type="number" min="0" max="30" step="0.01"')}${f('IRPF retention %','irpf_pct',i.irpf_pct,'type="number" min="0" max="30" step="0.01"')}
  </div><div class="form-actions"><button type="button" class="btn" data-action="close">Cancel</button><button type="submit" class="btn primary">Save</button></div></form>`,'BILLING');}
document.addEventListener('click',async event=>{const el=event.target.closest?.('[data-bill]');if(!el)return;const what=el.dataset.bill;
  try{if(what==='tab'){billingTab=el.dataset.tab;render();}else if(what==='invoice')openInvoice(el.dataset.key);else if(what==='issuer')openIssuer(el.dataset.id);
    else if(what==='cancel'){const i=billingData.invoices.find(x=>x.id===Number(el.dataset.id));if(!window.confirm(`Cancel invoice ${i.number}? It stays in the list as cancelled (its number is not used again) and its trips go back to Pending.`))return;
      el.disabled=true;const result=await api('/api/invoice-cancel',{id:i.id});await refresh();await loadBilling();toast(result.message);}}catch(error){toast(error.message,true);el.disabled=false;}});
document.addEventListener('change',async event=>{const input=event.target;if(input.dataset?.billFilter){if(input.dataset.billFilter==='month')billingMonth=input.value;render();return;}
  if(!input.dataset?.billField)return;try{await saveBillLine(input.closest('[data-line]'));}catch(error){toast(error.message,true);}});
document.addEventListener('input',event=>{const input=event.target;if(input.dataset?.billFilter==='q'){billingQuery=input.value;render();const el=$('#bill-q');if(el){el.focus();el.setSelectionRange(el.value.length,el.value.length);}return;}
  if(input.closest?.('#invoice-form'))invoiceTotals();});
document.addEventListener('submit',async event=>{const form=event.target;const formId=form.getAttribute?.('id');if(!['invoice-form','issuer-form'].includes(formId))return;event.preventDefault();
  if(form._busy)return;form._busy=true;const button=$('[type=submit]',form);
  try{if(button)button.disabled=true;if(formId==='invoice-form'){const g=billingData.pending.find(x=>x.key===form.dataset.key),data=formObject(form);const result=await api('/api/invoice-create',{...data,order_ids:g.lines.map(l=>l.order_id)});
      $('#modal').close();await refresh();await loadBilling();toast(result.message);window.open('/api/invoice.pdf?id='+result.id,'_blank','noopener');}
    else{const data=formObject(form);if(data.id)data.id=Number(data.id);else delete data.id;const result=await api('/api/billing-issuer',data);$('#modal').close();await loadBilling();toast(result.message);}}
  catch(error){showError(form,error);}finally{form._busy=false;if(button)button.disabled=false;}});
