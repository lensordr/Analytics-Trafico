// Client order page: show the address box for "other", and only the destinations of the chosen loading point.
(function(){
  const from=document.getElementById('from_id'),to=document.getElementById('to_id'),hint=document.getElementById('to-hint');
  if(!from||!to)return;
  function other(select){const box=document.querySelector('[data-other="'+select.id+'"]');if(!box)return;const on=select.value==='other';box.hidden=!on;box.querySelector('textarea').required=on;}
  function filter(){let shown=0;for(const option of to.options){const list=option.dataset.from;if(!list)continue;const ok=!from.value||from.value==='other'||list.split(' ').includes(from.value);option.hidden=!ok;option.disabled=!ok;if(ok)shown++;}
    if(to.selectedOptions[0]?.disabled)to.value='';hint.hidden=!from.value||from.value==='other';}
  from.addEventListener('change',()=>{other(from);filter();});to.addEventListener('change',()=>other(to));
  other(from);other(to);filter();
})();
