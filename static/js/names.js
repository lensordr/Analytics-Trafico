// ---------------------------------------------------------------- Fix capitals of company and town names
// "FERIMET S.L" → "Ferimet S.L.", "Caldes de montbui" → "Caldes de Montbui". The dispatcher reviews every change
// (and can correct it) before saving; only capitals and dots can change, never the name itself.
let nameFixList=[];
async function openNameFixes(){const result=await api('/api/name-fixes',{});nameFixList=result.fixes;
  if(!nameFixList.length){showModal('Fix capitals',notice('✓ Every company and town is already written properly.'),'SAVED LOCATIONS');return;}
  const field=(f,key)=>f[key]?`<div class="nf-pair"><span class="nf-before">${esc(f[key].before)}</span><span class="nf-arrow">→</span><input data-nf-id="${f.id}" data-nf-key="${key}" value="${esc(f[key].after)}" aria-label="${key==='company'?'Company':'Town'}"></div>`:'';
  showModal('Fix capitals · '+nameFixList.length+' places',`<p class="subtitle">Companies and towns written all in capitals or all in small letters, and legal forms (SL → S.L., SA → S.A.). Correct any line by hand (for example a brand written in capitals, like CELSA) or untick it. Only capitals and dots can change here.</p>
  ${notice('This changes the saved places for everyone (orders, routes, client links show the new spelling). Download a backup first: Settings & backup → Download backup.',true)}
  <div class="nf-tools"><input type="search" id="nf-search" placeholder="Search…" aria-label="Search names"><label class="checkbox"><input type="checkbox" id="nf-all" checked><span>Select all</span></label></div>
  <div class="nf-list">${nameFixList.map(f=>`<label class="nf-row" data-nf-row="${f.id}"><input type="checkbox" data-nf-check="${f.id}" checked><div>${field(f,'company')}${field(f,'city')}</div></label>`).join('')}</div>
  <div class="form-actions"><button class="btn" data-action="close">Cancel</button><button class="btn primary" data-action="name-fixes-apply">Save the ticked names</button></div>`,'SAVED LOCATIONS / NAMES');}
function nameFixFilter(text){const words=searchNorm(text).split(' ').filter(Boolean);for(const row of document.querySelectorAll('.nf-row')){const hay=searchNorm(row.textContent+' '+[...row.querySelectorAll('input[data-nf-key]')].map(i=>i.value).join(' '));row.hidden=!words.every(w=>hay.includes(w));}}
async function applyNameFixes(button){const items=[];for(const check of document.querySelectorAll('[data-nf-check]')){if(!check.checked)continue;const id=Number(check.dataset.nfCheck),entry={id};
    for(const input of document.querySelectorAll(`[data-nf-id="${id}"]`))entry[input.dataset.nfKey]=input.value;items.push(entry);}
  if(!items.length){toast('Tick at least one name.',true);return;}
  if(!confirm(`Save the new spelling of ${items.length} places? Did you download a backup first?`))return;
  button.disabled=true;try{const result=await api('/api/name-fixes-apply',{items});$('#modal').close();await refresh();toast(result.message);}catch(error){button.disabled=false;toast(error.message,true);}}
