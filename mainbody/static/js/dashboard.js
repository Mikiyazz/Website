/* 首页仪表盘与管理面板交互脚本（零外部 CDN 依赖，支持校园网/内网离线环境） */
(function() {
  // 审核/管理员端四段进度条比例与分布柱状图暖黄峰值高亮
  function initAdminBentoVisuals() {
    var segBar = document.getElementById("dash-segmented-bar");
    if (segBar) {
      var counts = {
        first: parseInt(segBar.getAttribute("data-c-first") || "0", 10) || 0,
        final: parseInt(segBar.getAttribute("data-c-final") || "0", 10) || 0,
        approved: parseInt(segBar.getAttribute("data-c-approved") || "0", 10) || 0,
        returned: parseInt(segBar.getAttribute("data-c-returned") || "0", 10) || 0
      };
      var sum = counts.first + counts.final + counts.approved + counts.returned;
      ["first", "final", "approved", "returned"].forEach(function(k) {
        var col = segBar.querySelector('[data-seg-col="' + k + '"]');
        var txt = segBar.querySelector('[data-seg-text="' + k + '"]');
        var pct = sum > 0 ? Math.round((counts[k] / sum) * 100) : 0;
        if (col) {
          col.style.flexGrow = String(sum > 0 ? Math.max(14, pct) : 25);
        }
        if (txt) {
          txt.textContent = sum > 0 ? (pct + "% (" + counts[k] + ")") : "0%";
        }
      });
    }

    var chart = document.getElementById("dash-distribution-chart");
    if (chart) {
      var cols = Array.prototype.slice.call(chart.querySelectorAll(".bento-bar-col"));
      var maxVal = 0;
      cols.forEach(function(c) {
        var v = parseInt(c.getAttribute("data-bar-count") || "0", 10) || 0;
        if (v > maxVal) { maxVal = v; }
      });
      var peakMarked = false;
      cols.forEach(function(c) {
        var v = parseInt(c.getAttribute("data-bar-count") || "0", 10) || 0;
        var fill = c.querySelector(".bento-bar-fill");
        var pct = maxVal > 0 ? Math.max(16, Math.round((v / maxVal) * 100)) : 18;
        if (fill) { fill.style.height = pct + "%"; }
        if (maxVal > 0 && v === maxVal && !peakMarked) {
          c.classList.add("is-peak");
          peakMarked = true;
        }
      });
    }
  }

  // 审核员/老师抽屉展开与打回修改意见 + 已通过成果二次确认前端校验
  window.toggleReviewDrawer = function(submissionId) {
    var row = document.getElementById("drawer-row-" + submissionId);
    if (!row) return;
    row.style.display = (row.style.display === "none" || !row.style.display) ? "table-row" : "none";
  };

  window.setReviewDecision = function(formEl, decisionVal) {
    if (!formEl) return;
    var decInput = formEl.querySelector(".decision-input");
    if (decInput) decInput.value = decisionVal;
    var confirmInput = formEl.querySelector(".confirm-return-input");
    if (confirmInput && decisionVal !== "returned") {
      confirmInput.value = "";
    }
  };

  var pendingDashReturnForm = null;

  window.validateReviewActionForm = function(formEl) {
    if (!formEl) return true;
    var decInput = formEl.querySelector(".decision-input");
    var commentEl = formEl.querySelector('textarea[name="comment"]');
    var confirmInput = formEl.querySelector(".confirm-return-input");
    var decision = decInput ? decInput.value : "approved";
    var comment = commentEl ? commentEl.value.trim() : "";

    if (decision === "returned" && !comment) {
      window.alert("打回修改时必须填写打回意见，原因不能为空！");
      if (commentEl) commentEl.focus();
      return false;
    }

    var needsConfirm = formEl.getAttribute("data-requires-confirm") === "true";
    if (decision === "returned" && needsConfirm && (!confirmInput || confirmInput.value !== "1")) {
      var statusLabel = formEl.getAttribute("data-status-label") || "已通过";
      var projName = formEl.getAttribute("data-project-name") || "该申报";
      var tip = "「" + projName + "」当前为【" + statusLabel + "】，打回后学生将恢复修改权限并需重新审核，确定打回吗？";
      var modalEl = document.getElementById("dashReturnConfirmModal");
      if (modalEl && window.bootstrap && window.bootstrap.Modal) {
        var descEl = document.getElementById("dashReturnConfirmDesc");
        var cmtEl = document.getElementById("dashReturnConfirmComment");
        if (descEl) { descEl.textContent = tip; }
        if (cmtEl) { cmtEl.textContent = "打回意见：" + comment; }
        pendingDashReturnForm = formEl;
        window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
        return false;
      }
      if (!window.confirm(tip)) {
        return false;
      }
      if (confirmInput) { confirmInput.value = "1"; }
    }
    return true;
  };

  function initDashReturnConfirmModal() {
    var confirmBtn = document.getElementById("btnConfirmDashReturn");
    if (!confirmBtn) return;
    confirmBtn.addEventListener("click", function() {
      var modalEl = document.getElementById("dashReturnConfirmModal");
      if (modalEl && window.bootstrap && window.bootstrap.Modal) {
        window.bootstrap.Modal.getOrCreateInstance(modalEl).hide();
      }
      if (pendingDashReturnForm) {
        var confirmInput = pendingDashReturnForm.querySelector(".confirm-return-input");
        if (confirmInput) { confirmInput.value = "1"; }
        var targetForm = pendingDashReturnForm;
        pendingDashReturnForm = null;
        targetForm.submit();
      }
    });
  }

  function initCategoryConfigPreview() {
    var input = document.getElementById("admin_act_categories");
    var preview = document.getElementById("admin-categories-preview");
    var resetBtn = document.getElementById("btn-reset-default-categories");
    var modeBadge = document.getElementById("admin-categories-mode-badge");
    if (!input || !preview) return;

    var defaultRaw = resetBtn ? (resetBtn.getAttribute("data-default-categories") || "") : "";
    function parseList(raw) {
      var parts = String(raw || "").split(/[\r\n,，;；、]+/);
      var out = [];
      var seen = {};
      parts.forEach(function(part) {
        var text = part.trim();
        if (text && !seen[text]) {
          seen[text] = true;
          out.push(text);
        }
      });
      return out;
    }

    var defaultList = parseList(defaultRaw);
    function renderPreview() {
      var parsed = parseList(input.value);
      var effective = parsed.length ? parsed : defaultList;
      var isCustom = parsed.length > 0 && parsed.join("\n") !== defaultList.join("\n");
      while (preview.firstChild) {
        preview.removeChild(preview.firstChild);
      }
      var label = document.createElement("span");
      label.className = "small text-secondary me-1";
      label.id = "admin-categories-count";
      label.textContent = (parsed.length ? "当前共 " : "留空将使用默认 ") + effective.length + " 项：";
      preview.appendChild(label);
      effective.forEach(function(cat) {
        var badge = document.createElement("span");
        badge.className = "badge badge-draft";
        badge.textContent = cat;
        preview.appendChild(badge);
      });
      if (modeBadge) {
        modeBadge.textContent = isCustom ? "已自定义" : "默认类别";
        modeBadge.className = "badge ms-1 " + (isCustom ? "badge-pending_final_review" : "badge-draft");
      }
    }

    input.addEventListener("input", renderPreview);
    if (resetBtn) {
      resetBtn.addEventListener("click", function() {
        input.value = defaultRaw;
        renderPreview();
        input.focus();
      });
    }
  }

  function initReviewQueueLiveFilter() {
    var form = document.getElementById("review-filter-form");
    var table = document.getElementById("review-submissions-table");
    if (!form || !table) return;
    var filterAbort = null;

    function buildFilterUrl() {
      var base = form.getAttribute("action") || window.location.pathname;
      var params = new URLSearchParams();
      Array.prototype.forEach.call(form.querySelectorAll("select[name], input[name]"), function(el) {
        var name = el.getAttribute("name");
        var val = el.value != null ? String(el.value).trim() : "";
        if (name && val !== "") {
          params.set(name, val);
        }
      });
      var qs = params.toString();
      return qs ? (base + "?" + qs) : base;
    }

    function refreshTable(targetUrl, activityChanged) {
      var tbody = table.querySelector("tbody");
      if (!tbody) {
        window.location.href = targetUrl;
        return;
      }
      if (filterAbort && typeof filterAbort.abort === "function") {
        filterAbort.abort();
      }
      filterAbort = typeof window.AbortController === "function" ? new window.AbortController() : null;
      tbody.style.transition = "opacity 0.15s ease";
      tbody.style.opacity = "0.55";

      fetch(targetUrl, {
        method: "GET",
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        signal: filterAbort ? filterAbort.signal : undefined
      }).then(function(resp) {
        if (!resp.ok || (resp.redirected && resp.url && resp.url.indexOf("/accounts/login/") !== -1)) {
          throw new Error("HTTP " + resp.status);
        }
        return resp.text();
      }).then(function(html) {
        var doc = new window.DOMParser().parseFromString(html, "text/html");
        var nextTbody = doc.querySelector("#review-submissions-table tbody");
        if (!nextTbody) {
          throw new Error("missing tbody");
        }
        tbody.innerHTML = nextTbody.innerHTML;
        var curTiles = document.getElementById("review-status-tiles");
        var nextTiles = doc.getElementById("review-status-tiles");
        if (curTiles && nextTiles) {
          curTiles.innerHTML = nextTiles.innerHTML;
        }
        var curSubPager = document.getElementById("review-submissions-pagination");
        var nextSubPager = doc.getElementById("review-submissions-pagination");
        if (curSubPager && nextSubPager) {
          curSubPager.innerHTML = nextSubPager.innerHTML;
        }
        if (activityChanged) {
          window.location.href = targetUrl;
          return;
        }
        if (window.history && typeof window.history.replaceState === "function") {
          window.history.replaceState(null, "", targetUrl);
        }
        tbody.style.opacity = "";
      })["catch"](function(err) {
        if (err && err.name === "AbortError") return;
        tbody.style.opacity = "";
        window.location.href = targetUrl;
      });
    }

    form.addEventListener("change", function(evt) {
      var target = evt.target;
      if (!target || target.tagName !== "SELECT") return;
      var isActivity = target.id === "filter_activity_id";
      if (isActivity) {
        var base = form.getAttribute("action") || window.location.pathname;
        var actVal = String(target.value || "").trim();
        window.location.href = actVal ? (base + "?activity_id=" + encodeURIComponent(actVal)) : base;
        return;
      }
      refreshTable(buildFilterUrl(), false);
    });

    form.addEventListener("submit", function(evt) {
      evt.preventDefault();
      refreshTable(buildFilterUrl(), false);
    });

    var subPager = document.getElementById("review-submissions-pagination");
    if (subPager) {
      subPager.addEventListener("click", function(evt) {
        var link = evt.target && evt.target.closest ? evt.target.closest("a.page-link") : null;
        if (!link) return;
        evt.preventDefault();
        refreshTable(link.getAttribute("href"), false);
      });
    }

    var resetBtn = document.getElementById("btn-review-filter-reset");
    if (resetBtn) {
      resetBtn.addEventListener("click", function(evt) {
        evt.preventDefault();
        Array.prototype.forEach.call(form.querySelectorAll("select[name]"), function(el) {
          if (el.id !== "filter_activity_id") {
            el.value = "";
          }
        });
        refreshTable(buildFilterUrl(), false);
      });
    }

    var tilesBox = document.getElementById("review-status-tiles");
    if (tilesBox) {
      tilesBox.addEventListener("click", function(evt) {
        var link = evt.target && evt.target.closest ? evt.target.closest("a.activity-item") : null;
        if (!link) return;
        evt.preventDefault();
        var statusVal = link.getAttribute("data-tile-status") || "";
        var statusSelect = document.getElementById("filter_status");
        if (statusSelect) {
          statusSelect.value = statusVal;
        }
        refreshTable(buildFilterUrl(), false);
      });
    }
  }

  function initAccountProgressLiveFilter() {
    var form = document.getElementById("account-progress-filter-form");
    var table = document.getElementById("account-progress-table");
    if (!form || !table) return;
    var abortCtrl = null;
    var searchTimer = null;

    function buildAccountFilterUrl() {
      var base = form.getAttribute("action") || window.location.pathname;
      var params = new URLSearchParams(window.location.search || "");
      ["acct_submit", "acct_class", "acct_decl_type", "acct_q", "acct_page"].forEach(function(k) {
        params.delete(k);
      });
      Array.prototype.forEach.call(form.querySelectorAll("select[name], input[name]"), function(el) {
        var name = el.getAttribute("name");
        var val = el.value != null ? String(el.value).trim() : "";
        if (name && val !== "") {
          params.set(name, val);
        }
      });
      var qs = params.toString();
      return qs ? (base + "?" + qs) : base;
    }

    function refreshAccountTable(targetUrl) {
      var tbody = table.querySelector("tbody");
      if (!tbody) {
        window.location.href = targetUrl;
        return;
      }
      if (abortCtrl && typeof abortCtrl.abort === "function") {
        abortCtrl.abort();
      }
      abortCtrl = typeof window.AbortController === "function" ? new window.AbortController() : null;
      tbody.style.transition = "opacity 0.15s ease";
      tbody.style.opacity = "0.55";

      fetch(targetUrl, {
        method: "GET",
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        signal: abortCtrl ? abortCtrl.signal : undefined
      }).then(function(resp) {
        if (!resp.ok || (resp.redirected && resp.url && resp.url.indexOf("/accounts/login/") !== -1)) {
          throw new Error("HTTP " + resp.status);
        }
        return resp.text();
      }).then(function(html) {
        var doc = new window.DOMParser().parseFromString(html, "text/html");
        var nextTbody = doc.querySelector("#account-progress-table tbody");
        if (!nextTbody) {
          throw new Error("missing account tbody");
        }
        tbody.innerHTML = nextTbody.innerHTML;
        var curBadges = document.getElementById("account-progress-count-badges");
        var nextBadges = doc.getElementById("account-progress-count-badges");
        if (curBadges && nextBadges) {
          curBadges.innerHTML = nextBadges.innerHTML;
        }
        var curPills = document.getElementById("account-progress-quick-pills");
        var nextPills = doc.getElementById("account-progress-quick-pills");
        if (curPills && nextPills) {
          curPills.innerHTML = nextPills.innerHTML;
        }
        var curAcctPager = document.getElementById("account-progress-pagination");
        var nextAcctPager = doc.getElementById("account-progress-pagination");
        if (curAcctPager && nextAcctPager) {
          curAcctPager.innerHTML = nextAcctPager.innerHTML;
        }
        if (window.history && typeof window.history.replaceState === "function") {
          window.history.replaceState(null, "", targetUrl);
        }
        tbody.style.opacity = "";
      })["catch"](function(err) {
        if (err && err.name === "AbortError") return;
        tbody.style.opacity = "";
        window.location.href = targetUrl;
      });
    }

    var acctPager = document.getElementById("account-progress-pagination");
    if (acctPager) {
      acctPager.addEventListener("click", function(evt) {
        var link = evt.target && evt.target.closest ? evt.target.closest("a.page-link") : null;
        if (!link) return;
        evt.preventDefault();
        refreshAccountTable(link.getAttribute("href"));
      });
    }

    form.addEventListener("change", function(evt) {
      var target = evt.target;
      if (!target || target.tagName !== "SELECT") return;
      refreshAccountTable(buildAccountFilterUrl());
    });

    form.addEventListener("submit", function(evt) {
      evt.preventDefault();
      refreshAccountTable(buildAccountFilterUrl());
    });

    var searchInput = document.getElementById("filter_acct_q");
    if (searchInput) {
      searchInput.addEventListener("input", function() {
        if (searchTimer) { window.clearTimeout(searchTimer); }
        searchTimer = window.setTimeout(function() {
          refreshAccountTable(buildAccountFilterUrl());
        }, 250);
      });
    }

    var resetBtn = document.getElementById("btn-acct-filter-reset");
    if (resetBtn) {
      resetBtn.addEventListener("click", function(evt) {
        evt.preventDefault();
        Array.prototype.forEach.call(form.querySelectorAll("select[name], input[type='text']"), function(el) {
          el.value = "";
        });
        refreshAccountTable(buildAccountFilterUrl());
      });
    }

    var quickPills = document.getElementById("account-progress-quick-pills");
    if (quickPills) {
      quickPills.addEventListener("click", function(evt) {
        var btn = evt.target && evt.target.closest ? evt.target.closest("[data-acct-quick]") : null;
        if (!btn) return;
        evt.preventDefault();
        var val = btn.getAttribute("data-acct-quick") || "";
        var sel = document.getElementById("filter_acct_submit");
        if (sel) { sel.value = val; }
        refreshAccountTable(buildAccountFilterUrl());
      });
    }
  }

  function initDashboardBulkExport() {
    var btn = document.getElementById("btn-dash-start-export");
    var modeSel = document.getElementById("dash-export-mode-select");
    var statusBox = document.getElementById("dash-export-status-box");
    var statusText = document.getElementById("dash-export-status-text");
    var downloadLink = document.getElementById("dash-export-download-link");
    if (!btn || !statusBox || !statusText || !downloadLink) return;

    var csrfToken = (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || "";
    var pollTimer = null;

    function stopPoll() {
      if (pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function renderJob(job) {
      statusBox.hidden = false;
      var pct = Number(job.percent || 0);
      var modeLabel = job.mode_label ? (job.mode_label + " · ") : "";
      if (job.status === "done") {
        stopPoll();
        if (job.file_count) {
          statusText.textContent = modeLabel + "已完成（" + job.file_count + " 份文件）";
        } else {
          statusText.textContent = modeLabel + "已完成（暂无可导出文件，0 份）";
        }
        if (job.download_url) {
          downloadLink.href = job.download_url;
          downloadLink.hidden = false;
        }
      } else if (job.status === "failed") {
        stopPoll();
        statusText.textContent = modeLabel + "导出失败：" + (job.error_code || "未知错误");
        downloadLink.hidden = true;
      } else {
        statusText.textContent = modeLabel + (job.status_display || "处理中") + "（" + pct + "%）";
        downloadLink.hidden = true;
      }
    }

    function poll(jobId) {
      fetch("/api/v1/exports/status/" + jobId + "/", {
        headers: { "Accept": "application/json" },
        credentials: "same-origin"
      }).then(function(resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      }).then(function(data) {
        renderJob(data.job || {});
      })["catch"](function() {
        stopPoll();
        statusText.textContent = "进度查询失败，请稍后刷新查看导出任务。";
      });
    }

    btn.addEventListener("click", function() {
      var activityId = btn.getAttribute("data-activity-id") || "";
      if (!activityId) return;
      var mode = modeSel ? modeSel.value : "declaration_and_outcomes";
      stopPoll();
      btn.disabled = true;
      if (modeSel) modeSel.disabled = true;
      statusBox.hidden = false;
      downloadLink.hidden = true;
      statusText.textContent = "正在创建导出任务…";

      fetch(btn.getAttribute("data-start-url"), {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
          "Accept": "application/json"
        },
        body: JSON.stringify({ activity_id: activityId, mode: mode })
      }).then(function(resp) {
        return resp.json().then(function(data) { return { ok: resp.ok, data: data }; });
      }).then(function(result) {
        btn.disabled = false;
        if (modeSel) modeSel.disabled = false;
        if (!result.ok) {
          var msg = (result.data && result.data.error && result.data.error.message) || "导出任务创建失败。";
          statusText.textContent = msg;
          return;
        }
        var job = result.data.job || {};
        renderJob(job);
        if (job.status !== "done" && job.status !== "failed") {
          var jobId = result.data.job_id || job.job_id;
          pollTimer = window.setInterval(function() { poll(jobId); }, 2000);
        }
      })["catch"](function() {
        btn.disabled = false;
        if (modeSel) modeSel.disabled = false;
        statusText.textContent = "网络异常，导出任务未能创建，请重试。";
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function() {
    initAdminBentoVisuals();
    initCategoryConfigPreview();
    initReviewQueueLiveFilter();
    initAccountProgressLiveFilter();
    initDashboardBulkExport();
    initDashReturnConfirmModal();
  });
})();
