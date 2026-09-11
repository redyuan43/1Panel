/* Browser-local view preferences. Never store credentials or editing forms. */
window.ViewPreferences = (() => {
  const key = "ai-router-view-preferences-v1";
  let data = {};
  try { const value = JSON.parse(localStorage.getItem(key) || "{}"); if (value && typeof value === "object" && !Array.isArray(value)) data = value; } catch (_) {}
  function get(name, fallback) { return Object.hasOwn(data, name) ? data[name] : fallback; }
  function put(name, value) { data[name] = value; try { localStorage.setItem(key, JSON.stringify(data)); } catch (_) {} }
  function state(namespace, defaults, rules) {
    const valid = (k,v) => Array.isArray(rules[k]) ? rules[k].includes(v) : rules[k] === "id" ? v === null || typeof v === "string" && v.length <= 200 : typeof v === typeof defaults[k];
    for (const k of Object.keys(rules)) { const v=get(namespace+"."+k, defaults[k]); if(valid(k,v)) defaults[k]=v; }
    return new Proxy(defaults, {set(obj,k,v) { obj[k]=v; if(Object.hasOwn(rules,k)&&valid(k,v))put(namespace+"."+k,v); return true; }});
  }
  const bound = new WeakSet();
  const savers = new Map();
  function fields(selectors) {
    for(const selector of selectors) {
      const el=document.querySelector(selector); if(!el||bound.has(el)) continue;bound.add(el);
      const name="field."+location.pathname+"."+selector;
      const apply=()=>{const v=get(name,null);if(v===null)return false;
        if(el.type==="checkbox") { if(typeof v==="boolean")el.checked=v; return false; }
        if(typeof v!=="string"||v.length>500)return false;
        if(el.tagName==="SELECT"&&![...el.options].some(o=>o.value===v))return false;
        const changed=el.value!==v;el.value=v;return changed;};
      apply();
      const save=()=>put(name,el.type==="checkbox"?el.checked:el.value);savers.set(selector,save);
      el.addEventListener("change",save,true);
      if(el.tagName==="SELECT")new MutationObserver(()=>{if(apply())el.dispatchEvent(new Event("change",{bubbles:true}));}).observe(el,{childList:true});
    }
  }
  function details() {
    const bind=()=>document.querySelectorAll("details[id],details[data-cache-detail]").forEach(el=>{
      if(el.dataset.preferenceBound)return;el.dataset.preferenceBound="true";
      const name="detail."+location.pathname+"."+(el.id||el.dataset.cacheDetail);
      const saved=get(name,null);if(typeof saved==="boolean")el.open=saved;
      el.addEventListener("toggle",()=>put(name,el.open));
    });bind();new MutationObserver(bind).observe(document.body,{childList:true,subtree:true});
  }
  function sortEndpoints(rows, sort, direction) {
    if(sort==="default")return [...rows];
    const value=r=>({node:r.endpoint.node,model:r.endpoint.public_model,status:!r.endpoint.enabled?0:r.status.healthy?2:1,context:r.status.eligible_context_tokens||r.endpoint.safe_context_tokens,load:r.status.healthy?r.status.load_headroom:null})[sort];
    return [...rows].sort((a,b)=>{const x=value(a),y=value(b);if(x==null||y==null)return x==null?(y==null?0:1):-1;
      const cmp=typeof x==="number"&&typeof y==="number"?x-y:String(x).localeCompare(String(y),"zh-CN",{numeric:true});
      return cmp*(direction==="desc"?-1:1)||a.endpoint.id.localeCompare(b.endpoint.id);});
  }
  return {get,put,state,fields,details,sortEndpoints,capture:selectors=>selectors.forEach(s=>savers.get(s)?.())};
})();
