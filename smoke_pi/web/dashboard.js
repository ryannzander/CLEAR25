/* ============================================================
   CLEAR · Smoke Collector dashboard
   Reads data/{status,history,latest_<source>}.json written by collector.py
   and renders live telemetry + the latest ON+QC smoke raster.
   ============================================================ */
(function () {
  "use strict";

  var REFRESH_MS = 30000;
  var SRC = {
    firework: { label: "FireWork", sub: "ECCC RAQDPS · GRIB2", cadence_h: 12 },
    bluesky:  { label: "BlueSky",  sub: "firesmoke.ca · NetCDF", cadence_h: 6 },
  };
  var RAMP = [[0,[46,204,113]],[12,[241,196,15]],[35,[230,126,34]],
              [55,[231,76,60]],[150,[142,68,173]],[250,[126,0,35]]];

  var map, overlay = null, curSrc = "firework";
  var latestCache = {};

  // ---- helpers ----
  function $(id){ return document.getElementById(id); }
  function el(tag, cls, html){ var e=document.createElement(tag); if(cls)e.className=cls; if(html!=null)e.innerHTML=html; return e; }
  function getJSON(p){ return fetch(p+"?t="+Date.now()).then(function(r){ return r.ok?r.json():null; }).catch(function(){ return null; }); }
  function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }

  function rampColor(pm){
    if(pm<=RAMP[0][0]) return RAMP[0][1];
    for(var i=1;i<RAMP.length;i++){ if(pm<=RAMP[i][0]){
      var a=RAMP[i-1],b=RAMP[i],t=(pm-a[0])/(b[0]-a[0]);
      return [Math.round(a[1][0]+t*(b[1][0]-a[1][0])),Math.round(a[1][1]+t*(b[1][1]-a[1][1])),Math.round(a[1][2]+t*(b[1][2]-a[1][2]))];
    }}
    return RAMP[RAMP.length-1][1];
  }
  function ago(iso){
    if(!iso) return "—";
    var s=Math.max(0,(Date.now()-new Date(iso).getTime())/1000);
    if(s<60) return Math.round(s)+"s ago";
    if(s<3600) return Math.round(s/60)+"m ago";
    if(s<86400) return Math.round(s/3600)+"h ago";
    return Math.round(s/86400)+"d ago";
  }
  function clock(iso){ if(!iso) return "—"; var d=new Date(iso); return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}); }
  function fmtRun(r){ if(!r) return "—"; var m=/^(\d{4})(\d{2})(\d{2})T?(\d{2})/.exec(r); return m?(m[2]+"/"+m[3]+" "+m[4]+"Z"):r; }

  // ---- header / overall status ----
  function renderHeader(st){
    $("host").textContent = (st && st.host) || "—";
    $("updated").textContent = st ? ago(st.updated_at) : "—";
    var chip=$("overall-status"), lbl=$("overall-label");
    chip.className="status-chip";
    if(!st){ lbl.textContent="no signal"; chip.classList.add("down"); return; }
    var ageMin=(Date.now()-new Date(st.updated_at).getTime())/60000;
    var anyFail=Object.keys(st.sources||{}).some(function(k){ return st.sources[k].ok===false; });
    if(ageMin>180){ chip.classList.add("stale"); lbl.textContent="stale ("+Math.round(ageMin/60)+"h)"; }
    else if(anyFail){ chip.classList.add("stale"); lbl.textContent="degraded"; }
    else { chip.classList.add("live"); lbl.textContent="collecting"; }
  }

  // ---- source cards ----
  function sparkline(series){
    var w=300,h=34,pad=2, svg='<svg class="spark" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none">';
    if(series.length<2) return svg+'</svg>';
    var max=Math.max.apply(null,series.concat([1])), n=series.length;
    var pts=series.map(function(v,i){ var x=pad+(w-2*pad)*i/(n-1); var y=h-pad-(h-2*pad)*(v/max); return x.toFixed(1)+","+y.toFixed(1); });
    var area="M"+pad+","+(h-pad)+" L"+pts.join(" L")+" L"+(w-pad)+","+(h-pad)+" Z";
    svg+='<path d="'+area+'" fill="rgba(255,122,61,0.12)"/>';
    svg+='<polyline points="'+pts.join(" ")+'" fill="none" stroke="#ff7a3d" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>';
    var lx=pts[pts.length-1].split(",");
    svg+='<circle cx="'+lx[0]+'" cy="'+lx[1]+'" r="2.4" fill="#ff7a3d"/>';
    return svg+'</svg>';
  }

  function renderSources(st, hist){
    var wrap=$("sources"); wrap.innerHTML="";
    Object.keys(SRC).forEach(function(key){
      var meta=SRC[key], s=(st&&st.sources&&st.sources[key])||{};
      var cls = s.ok===false ? "fail" : (s.ok===true ? "ok" : "warn");
      var badge = s.ok===false ? "error" : (s.ok===true ? "online" : "idle");
      var next = s.last_success ? new Date(new Date(s.last_success).getTime()+(s.cadence_hours||meta.cadence_h)*3600000) : null;
      var nextLbl = next ? (next<Date.now() ? "due now" : "~"+clock(next.toISOString())) : "—";
      var series = (hist||[]).filter(function(e){ return e.source===key && e.ok!==false && e.max_pm!=null; }).map(function(e){ return e.max_pm; }).slice(-40);

      var c=el("div","src "+cls);
      c.innerHTML =
        '<div class="src-top"><div class="src-name"><span class="led"></span>'+meta.label+'</div>'+
        '<span class="src-badge">'+badge+'</span></div>'+
        '<div class="src-stats">'+
          stat("Last run", fmtRun(s.last_run), true)+
          stat("Peak PM2.5", (s.last_max_pm!=null? s.last_max_pm : "—"), true, "µg/m³")+
          stat("Collected", ago(s.last_success))+
          stat("Next", nextLbl)+
        '</div>'+
        (s.ok===false ? '<div class="src-err">'+esc(s.error||"unknown error")+'</div>' : sparkline(series))+
        '<div class="src-foot"><span>'+meta.sub+'</span><span class="mono">'+(s.slices||0)+' slices</span></div>';
      wrap.appendChild(c);
    });
  }
  function stat(lbl,val,big,unit){
    return '<div class="stat"><div class="lbl">'+lbl+'</div><div class="val'+(big?" big":"")+' mono">'+val+(unit?'<span class="u">'+unit+'</span>':"")+'</div></div>';
  }

  // ---- archive + feed ----
  function renderArchive(st){
    if(!st){ return; }
    $("arch-slices").textContent=(st.total_slices||0)+" slices";
    var d=st.disk||{}; var usedPct = (d.used_gb&&(d.used_gb+d.free_gb)) ? Math.round(100*d.used_gb/(d.used_gb+d.free_gb)) : 0;
    $("disk-label").textContent = (d.free_gb!=null? d.free_gb+" GB free" : "—");
    var bar=$("disk-bar"); bar.style.width=usedPct+"%"; bar.classList.toggle("high", usedPct>85);
    $("data-size").textContent = (d.data_mb!=null? d.data_mb+" MB" : "—");
    $("since").textContent = st.started_at ? new Date(st.started_at).toLocaleDateString([], {month:"short",day:"numeric"}) : "—";
  }
  function renderFeed(hist){
    var ul=$("events"); ul.innerHTML="";
    if(!hist||!hist.length){ ul.appendChild(el("li","evt-empty","no events yet")); $("feed-count").textContent="—"; return; }
    $("feed-count").textContent=hist.length+" events";
    hist.slice().reverse().slice(0,40).forEach(function(e){
      var li=el("li","evt");
      var ok=e.ok!==false;
      li.innerHTML='<span class="et">'+clock(e.t)+'</span>'+
        '<span class="em">'+(SRC[e.source]?'<b>'+SRC[e.source].label+'</b> ':"")+
        (ok ? (e.new? 'new run '+fmtRun(e.run)+' · '+ (e.cells||0)+' cells' : 'run '+fmtRun(e.run))
             : ('failed · '+esc(e.error||"error")))+'</span>'+
        '<span class="pill '+(ok?"ok":"fail")+'">'+(ok?(e.new?"saved":"ok"):"fail")+'</span>';
      ul.appendChild(li);
    });
  }

  // ---- map raster ----
  function meshBounds(s){ var b=s.bbox; return [[b.selat,b.nwlng],[b.nwlat,b.selng]]; }
  function rasterURL(s){
    var rows=s.rows, cols=s.cols, vals=s.values||[];
    var cv=document.createElement("canvas"); cv.width=cols; cv.height=rows;
    var ctx=cv.getContext("2d"), img=ctx.createImageData(cols,rows), d=img.data;
    for(var i=0;i<rows*cols;i++){ var v=vals[i], o=i*4;
      if(v==null || (typeof v==="number" && isNaN(v)) || v<1){ d[o+3]=0; continue; }
      var c=rampColor(v); var a=Math.min(0.9, 0.15+v/50);   // smoke pops, clean air transparent
      d[o]=c[0]; d[o+1]=c[1]; d[o+2]=c[2]; d[o+3]=Math.round(a*255);
    }
    ctx.putImageData(img,0,0); return cv.toDataURL();
  }
  function renderMap(s){
    var empty=$("map-empty");
    if(!s || !s.values){ empty.classList.remove("hidden"); $("me-text").textContent="No "+SRC[curSrc].label+" slice collected yet."; if(overlay){map.removeLayer(overlay);overlay=null;} $("frame-run").textContent="—"; $("frame-max").textContent="—"; return; }
    empty.classList.add("hidden");
    var url=rasterURL(s);
    if(!overlay){ overlay=L.imageOverlay(url, meshBounds(s), {opacity:1, interactive:false}).addTo(map); }
    else { overlay.setUrl(url); overlay.setBounds(meshBounds(s)); }
    map.fitBounds(meshBounds(s), {padding:[12,12], maxZoom:6, animate:false});
    $("frame-run").textContent="run "+fmtRun(s.run);
    $("frame-max").textContent="peak "+(s.max_pm!=null?s.max_pm:"—")+" µg/m³";
  }

  function switchSrc(src){
    curSrc=src;
    document.querySelectorAll(".seg-btn").forEach(function(b){ b.classList.toggle("active", b.dataset.src===src); });
    renderMap(latestCache[src]);
  }

  function initMap(){
    map=L.map("map",{preferCanvas:true, zoomControl:false, attributionControl:true}).setView([49,-77],4);
    L.control.zoom({position:"bottomright"}).addTo(map);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",{
      attribution:'&copy; OSM &copy; CARTO · smoke © BlueSky/ECCC', maxZoom:10 }).addTo(map);
  }

  function tick(){
    Promise.all([getJSON("data/status.json"), getJSON("data/history.json"),
                 getJSON("data/latest_firework.json"), getJSON("data/latest_bluesky.json")])
      .then(function(r){
        var st=r[0], hist=r[1]||[];
        latestCache.firework=r[2]; latestCache.bluesky=r[3];
        renderHeader(st); renderSources(st,hist); renderArchive(st); renderFeed(hist);
        // disable a source button if its slice is absent
        document.querySelectorAll(".seg-btn").forEach(function(b){ b.disabled = !latestCache[b.dataset.src]; });
        renderMap(latestCache[curSrc]);
      });
  }

  document.addEventListener("DOMContentLoaded", function(){
    initMap();
    document.getElementById("layer-seg").addEventListener("click", function(e){
      var b=e.target.closest(".seg-btn"); if(b && !b.disabled) switchSrc(b.dataset.src);
    });
    tick(); setInterval(tick, REFRESH_MS);
  });
})();
