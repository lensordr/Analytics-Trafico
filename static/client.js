// Client order page: search the places by company, town or postal code; usual routes fill both fields;
// only the destinations of the chosen loading point; the address box for "other".
(function(){
  const from=document.getElementById('from_id'),to=document.getElementById('to_id'),hint=document.getElementById('to-hint');
  if(!from||!to)return;
  const norm=s=>String(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase();
  const add=document.getElementById('add-route'),back=document.getElementById('back-routes'),hasRoutes=from.options.length>2;
  function other(select){const box=document.querySelector('[data-other="'+select.id+'"]');if(!box)return;const on=select.value==='other';box.hidden=!on;
    for(const input of box.querySelectorAll('[data-need]'))input.required=on;
    if(back)back.hidden=!hasRoutes||(from.value!=='other'&&to.value!=='other');}
  function allowed(option){const list=option.dataset.from;return !list||option.closest('select')!==to||!from.value||from.value==='other'||list.split(' ').includes(from.value);}
  function filter(){for(const option of to.options){if(!option.dataset.from)continue;const ok=allowed(option);option.hidden=!ok;option.disabled=!ok;}
    if(to.selectedOptions[0]&&to.selectedOptions[0].disabled)to.value='';hint.hidden=!from.value||from.value==='other';other(to);}
  function choose(select,value){select.value=value;select.dispatchEvent(new Event('change'));const box=document.querySelector('.finder[data-for="'+select.id+'"]');if(box){box.querySelector('input').value='';box.querySelector('.matches').hidden=true;box.querySelector('.no-match').hidden=true;}}
  for(const box of document.querySelectorAll('.finder')){
    const select=document.getElementById(box.dataset.for),input=box.querySelector('input'),list=box.querySelector('.matches'),none=box.querySelector('.no-match');
    input.addEventListener('input',()=>{const words=norm(input.value).split(/\s+/).filter(Boolean);list.innerHTML='';
      if(!words.length){list.hidden=true;none.hidden=true;return;}
      const found=[...select.options].filter(o=>o.dataset.search&&allowed(o)&&words.every(w=>norm(o.dataset.search+' '+o.textContent).split(/[^a-z0-9]+/).some(x=>x.startsWith(w)))).slice(0,8);
      for(const o of found){const b=document.createElement('button');b.type='button';b.className='match';b.textContent=o.textContent;b.addEventListener('click',()=>choose(select,o.value));list.appendChild(b);}
      list.hidden=!found.length;none.hidden=!!found.length;});
    input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();const first=list.querySelector('.match');if(first)first.click();}});}
  for(const b of document.querySelectorAll('.route-pick'))b.addEventListener('click',()=>{choose(from,b.dataset.from);choose(to,b.dataset.to);
    for(const x of document.querySelectorAll('.route-pick'))x.classList.toggle('on',x===b);document.getElementById('request-form').scrollIntoView({behavior:'smooth',block:'start'});});
  if(add)add.addEventListener('click',()=>{for(const s of [from,to])choose(s,'other');for(const x of document.querySelectorAll('.route-pick'))x.classList.remove('on');
    const first=document.querySelector('[data-other="from_id"] input');if(first)first.focus();});
  if(back)back.addEventListener('click',()=>{for(const s of [from,to])choose(s,'');from.focus();});
  if(!hasRoutes&&add)add.hidden=true;
  from.addEventListener('change',()=>{other(from);filter();});to.addEventListener('change',()=>other(to));
  other(from);other(to);filter();
})();
