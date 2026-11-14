/* sidequest frontend: Leaflet map + Chart.js elevation profile. */
(function () {
  "use strict";

  var currentMap = null;
  var currentChart = null;

  var CATEGORY_COLORS = {
    viewpoint: "#2b8fb8",
    peak: "#7c5bc7",
    lake: "#2f7fc9",
    historic: "#b97a12",
    mountain_pass: "#d24d63",
    mtb_trail: "#3d8f2e",
    mtb_route: "#3d8f2e",
    attraction: "#c94f8d",
    picnic_site: "#7b9a14",
    park: "#1f8f68",
    track: "#a98606",
    poi: "#5f7268",
  };

  function colorFor(category) {
    return CATEGORY_COLORS[category] || CATEGORY_COLORS.poi;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function haversineKm(a, b) {
    var toRad = Math.PI / 180;
    var R = 6371.0088;
    var dLat = (b[0] - a[0]) * toRad;
    var dLon = (b[1] - a[1]) * toRad;
    var lat1 = a[0] * toRad;
    var lat2 = b[0] * toRad;
    var h =
      Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  function downsample(geometry, elevations, maxPoints) {
    var count = geometry.length;
    if (count <= maxPoints) {
      return { geometry: geometry, elevations: elevations };
    }
    var step = count / maxPoints;
    var outGeo = [];
    var outEle = [];
    for (var i = 0; i < maxPoints; i++) {
      var idx = Math.floor(i * step);
      outGeo.push(geometry[idx]);
      outEle.push(elevations[idx] || 0);
    }
    outGeo.push(geometry[count - 1]);
    outEle.push(elevations[count - 1] || 0);
    return { geometry: outGeo, elevations: outEle };
  }

  function dotIcon(extraClass, color) {
    return L.divIcon({
      className: "marker-dot pulse " + extraClass,
      html: "",
      iconSize: [18, 18],
      iconAnchor: [9, 9],
      popupAnchor: [0, -12],
    });
  }

  function renderMap(plan) {
    var el = document.getElementById("route-map");
    if (!el) return;
    if (currentMap) {
      currentMap.remove();
      currentMap = null;
    }

    var coords = plan.geometry.map(function (pt) {
      return [pt[0], pt[1]];
    });
    if (!coords.length) return;

    var map = L.map(el, {
      zoomControl: true,
      scrollWheelZoom: true,
      worldCopyJump: true,
    });

    L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
      {
        maxZoom: 19,
        attribution:
          'Tiles &copy; <a href="https://www.esri.com/" target="_blank" rel="noopener">Esri</a> — Esri, HERE, Garmin, USGS',
      }
    ).addTo(map);

    // Dark casing keeps the lime route legible on the light topo map.
    L.polyline(coords, {
      color: "#10301a",
      weight: 9,
      opacity: 0.45,
      lineCap: "round",
      lineJoin: "round",
    }).addTo(map);

    var line = L.polyline(coords, {
      color: "#a8e63c",
      weight: 4.4,
      opacity: 1,
      lineCap: "round",
      lineJoin: "round",
    }).addTo(map);

    L.marker(coords[0], {
      icon: dotIcon("marker-start"),
      zIndexOffset: 500,
    })
      .addTo(map)
      .bindPopup("<strong>Start</strong><br>" + escapeHtml(plan.start_label || ""));

    if (!plan.round_trip) {
      L.marker(coords[coords.length - 1], {
        icon: dotIcon("marker-end"),
        zIndexOffset: 500,
      })
        .addTo(map)
        .bindPopup("<strong>Ziel</strong><br>" + escapeHtml(plan.end_label || ""));
    }

    (plan.highlights || []).forEach(function (highlight, index) {
      var marker = L.marker([highlight.lat, highlight.lon], {
        icon: L.divIcon({
          className: "poi-marker",
          html: String(index + 1),
          iconSize: [27, 27],
          iconAnchor: [13.5, 13.5],
          popupAnchor: [0, -15],
        }),
      }).addTo(map);

      var node = marker.getElement();
      if (node) {
        node.style.setProperty("--marker-color", colorFor(highlight.category));
      }

      marker.bindPopup(
        "<strong>" +
          (index + 1) +
          ". " +
          escapeHtml(highlight.name) +
          "</strong><br>" +
          escapeHtml(highlight.rationale || highlight.category)
      );
    });

    var routeBounds = line.getBounds();
    currentMap = map;

    // The map lives inside freshly swapped HTMX content. Fit only after the
    // browser has measured the new layout, otherwise Leaflet keeps a zero-size
    // view and zooms into the very first coordinate.
    function fitRoute() {
      if (currentMap !== map) return;
      map.invalidateSize();
      map.fitBounds(routeBounds, { padding: [30, 30] });
    }

    requestAnimationFrame(fitRoute);
    setTimeout(fitRoute, 180);
  }

  function renderChart(plan) {
    var canvas = document.getElementById("elevation-chart");
    if (!canvas || typeof Chart === "undefined") return;
    if (currentChart) {
      currentChart.destroy();
      currentChart = null;
    }

    var sampled = downsample(plan.geometry, plan.elevations, 400);
    var labels = [];
    var data = [];
    var cumulative = 0;
    for (var i = 0; i < sampled.geometry.length; i++) {
      if (i > 0) {
        cumulative += haversineKm(sampled.geometry[i - 1], sampled.geometry[i]);
      }
      labels.push(cumulative.toFixed(1));
      data.push(sampled.elevations[i]);
    }

    var elevBadge = document.getElementById("elev-range");
    if (elevBadge && data.length) {
      var min = Math.round(Math.min.apply(null, data));
      var max = Math.round(Math.max.apply(null, data));
      elevBadge.textContent = "Min " + min + " m · Max " + max + " m";
    }

    var ctx = canvas.getContext("2d");
    var height = (canvas.parentElement && canvas.parentElement.clientHeight) || 200;
    var fill = ctx.createLinearGradient(0, 0, 0, height);
    fill.addColorStop(0, "rgba(205, 245, 111, 0.4)");
    fill.addColorStop(0.55, "rgba(91, 220, 171, 0.13)");
    fill.addColorStop(1, "rgba(91, 220, 171, 0)");

    currentChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Höhe (m)",
            data: data,
            borderColor: "#cdf56f",
            backgroundColor: fill,
            borderWidth: 2.8,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: "#ecffc0",
            pointHoverBorderColor: "#0b140f",
            pointHoverBorderWidth: 2,
            fill: true,
            tension: 0.28,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 700 },
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            border: { display: false },
            ticks: {
              color: "#8aa393",
              font: { size: 10 },
              maxTicksLimit: 8,
              maxRotation: 0,
            },
            grid: { display: false },
          },
          y: {
            border: { display: false },
            ticks: {
              color: "#8aa393",
              font: { size: 10 },
              maxTicksLimit: 5,
              callback: function (value) {
                return value + " m";
              },
            },
            grid: { color: "rgba(238, 243, 231, 0.06)" },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "rgba(16, 26, 20, 0.96)",
            borderColor: "rgba(238, 243, 231, 0.13)",
            borderWidth: 1,
            titleColor: "#a4b7a9",
            bodyColor: "#dcff8c",
            bodyFont: { family: "Inter", size: 12, weight: "600" },
            titleFont: { family: "Inter", size: 10 },
            padding: 10,
            displayColors: false,
            callbacks: {
              title: function (items) {
                return items.length ? items[0].label + " km" : "";
              },
              label: function (item) {
                return Math.round(item.parsed.y) + " m";
              },
            },
          },
        },
      },
    });
  }

  function renderPlan(plan) {
    if (!plan) return;
    renderMap(plan);
    renderChart(plan);
  }

  /* ---------------------------------------------------------------------
   * NDJSON progress stream: live review log, then the final HTML result.
   * ------------------------------------------------------------------- */
  var streaming = false;

  function reviewLogHtml() {
    return (
      '<section class="panel review-log" aria-live="polite">' +
      '<div class="panel-head">' +
      '<div class="flex items-center gap-3">' +
      '<span class="section-icon" aria-hidden="true">' +
      '<svg viewBox="0 0 24 24" fill="none" class="h-5 w-5">' +
      '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3Z" ' +
      'stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" />' +
      "</svg></span>" +
      "<div><h3 class=\"font-display text-base font-semibold\">Streckenprüfung</h3>" +
      '<p class="text-xs text-sage">Status, Review und Korrekturen</p></div>' +
      "</div>" +
      '<span class="chip chip-lime">läuft …</span>' +
      "</div>" +
      '<div class="review-log-lines" id="review-log-lines"></div>' +
      "</section>"
    );
  }

  function ensureReviewLog() {
    var result = document.getElementById("result");
    if (!result) return null;
    result.innerHTML = reviewLogHtml();
    var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    result.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "start",
    });
    return document.getElementById("review-log-lines");
  }

  function appendReviewLog(kind, text) {
    var lines = document.getElementById("review-log-lines");
    if (!lines || !text) return;
    var line = document.createElement("div");
    line.className = "review-log-line review-log-" + kind;
    var dot = document.createElement("span");
    dot.className = "review-log-dot";
    dot.setAttribute("aria-hidden", "true");
    var message = document.createElement("span");
    message.textContent = text;
    line.appendChild(dot);
    line.appendChild(message);
    lines.appendChild(line);
  }

  function reviewEventText(event) {
    var parts = [
      "Review " + event.round + ": " + event.score + "/100",
      "Unsicherheit " + Math.round((event.uncertainty || 0) * 100) + " %",
    ];
    parts.push(event.accepted ? "passt" : "Korrektur nötig");
    return parts.join(" · ");
  }

  function errorPanel(message) {
    return (
      '<section class="panel"><div class="p-5 sm:p-6">' +
      '<p class="eyebrow">Fehler</p>' +
      '<h2 class="mt-2 font-display text-xl font-semibold">Route konnte nicht geladen werden</h2>' +
      '<p class="mt-2 text-sm leading-relaxed text-sage">' +
      escapeHtml(message) +
      "</p></div></section>"
    );
  }

  function finishStream(html) {
    var result = document.getElementById("result");
    if (result && html) {
      result.innerHTML = html;
      // Re-use the existing htmx hook so Leaflet/Chart.js render the result.
      result.dispatchEvent(
        new CustomEvent("htmx:afterSwap", {
          bubbles: true,
          detail: { target: result, elt: result },
        })
      );
    }
    cleanupStream();
  }

  function cleanupStream() {
    streaming = false;
    var button = document.getElementById("plan-submit");
    if (button) button.disabled = false;
    var form = document.querySelector('form[hx-post="/plan"]');
    if (form) form.removeAttribute("aria-busy");
  }

  function handleStreamEvent(event, state) {
    if (!event || !event.type) return;
    if (event.type === "status") {
      appendReviewLog("status", event.message);
    } else if (event.type === "review") {
      appendReviewLog(event.accepted ? "ok" : "warn", reviewEventText(event));
      var issues = (event.issues || [])
        .slice(0, 3)
        .map(function (issue) {
          return issue.description;
        })
        .filter(Boolean);
      if (issues.length) appendReviewLog("issue", issues.join(" · "));
    } else if (event.type === "revision") {
      appendReviewLog(
        "revision",
        "Korrektur: " + (event.message || "Route wird angepasst.") +
          (event.reason ? " — " + event.reason : "")
      );
    } else if (event.type === "result" || event.type === "error") {
      state.finished = true;
      finishStream(event.html);
    }
  }

  function streamPlan(form) {
    if (streaming) return;
    streaming = true;

    var button = document.getElementById("plan-submit");
    if (button) button.disabled = true;
    form.setAttribute("aria-busy", "true");
    ensureReviewLog();
    appendReviewLog("status", "Anfrage wird gesendet …");

    var state = { finished: false };
    var decoder = new TextDecoder();

    fetch("/plan", {
      method: "POST",
      body: new FormData(form),
      headers: { Accept: "application/x-ndjson" },
      credentials: "same-origin",
    })
      .then(function (response) {
        if (!response.ok) {
          return response.text().then(function (body) {
            var message = "HTTP " + response.status;
            try {
              var parsed = JSON.parse(body);
              if (parsed && parsed.detail) {
                message =
                  typeof parsed.detail === "string"
                    ? parsed.detail
                    : JSON.stringify(parsed.detail);
              }
            } catch (error) {
              /* keep the HTTP status */
            }
            throw new Error(message);
          });
        }
        var contentType = response.headers.get("content-type") || "";
        if (contentType.indexOf("application/x-ndjson") === -1 || !response.body) {
          return response.text().then(function (html) {
            state.finished = true;
            finishStream(html);
          });
        }

        var reader = response.body.getReader();
        var buffer = "";

        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) {
              if (!state.finished) {
                finishStream(
                  errorPanel("Der Prüf-Stream wurde ohne Ergebnis beendet.")
                );
              }
              return;
            }
            buffer += decoder.decode(chunk.value, { stream: true });
            var parts = buffer.split("\n");
            buffer = parts.pop();
            parts.forEach(function (line) {
              if (!line.trim()) return;
              try {
                handleStreamEvent(JSON.parse(line), state);
              } catch (error) {
                console.error("Could not parse review event", error, line);
              }
            });
            return pump();
          });
        }
        return pump();
      })
      .catch(function (error) {
        finishStream(
          errorPanel("Die Routenplanung ist fehlgeschlagen: " + error.message)
        );
      });
  }

  if (window.fetch && window.ReadableStream && window.TextDecoder) {
    var planForm = document.querySelector('form[hx-post="/plan"]');
    if (planForm) {
      planForm.addEventListener(
        "submit",
        function (event) {
          event.preventDefault();
          event.stopImmediatePropagation();
          streamPlan(planForm);
        },
        true
      );
    }
  }

  document.body.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    var holder = target ? target.querySelector("#plan-data") : null;

    if (target && target.id === "result") {
      var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      target.scrollIntoView({
        behavior: reduceMotion ? "auto" : "smooth",
        block: "start",
      });
    }

    if (!holder) return;
    try {
      renderPlan(JSON.parse(holder.textContent));
    } catch (error) {
      console.error("Could not render plan", error);
    }
  });
})();
