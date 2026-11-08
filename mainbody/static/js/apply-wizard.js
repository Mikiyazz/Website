/* 学生端「社会实践申报」四步向导 —— 前端骨架
 * 依赖：FilePond 4.32.x + 7 个插件 + zh-cn 语言包（全部本地 vendor，无 CDN）
 * 本文件只负责：步骤切换、团队成员行、FilePond 实例与校验、Step4 回显、提交动作。
 */
(function () {
  'use strict';

  var wizard = document.getElementById('apply-wizard');
  if (!wizard) {
    return;
  }

  var MATERIALS = readJson('wizard-materials-json', []);
  var INITIAL_MEMBERS = readJson('wizard-team-json', []);
  var SUBMIT_URL = wizard.dataset.submitUrl || '';
  var DETAIL_URL = wizard.dataset.detailUrl || '';
  var SUCCESS_URL = wizard.dataset.successUrl || '';
  var LOOKUP_URL = wizard.dataset.lookupUrl || '/api/students/lookup/';
  var CSRF_TOKEN = (document.getElementById('apply-csrf-token') || {}).value || '';
  var CAN_SUBMIT = wizard.dataset.canSubmit === 'true';
  // 上传通道：宣讲视频走 5MB 分片，其余材料单请求直传（服务端 URL 全部由模板注入）。
  var CHUNK_START_URL = wizard.dataset.chunkStartUrl || '/api/v1/uploads/chunk-start/';
  var CHUNK_UPLOAD_URL = wizard.dataset.chunkUploadUrl || '/api/v1/uploads/chunk-upload/';
  var CHUNK_COMPLETE_URL = wizard.dataset.chunkCompleteUrl || '/api/v1/uploads/chunk-complete/';
  var DIRECT_UPLOAD_URL = wizard.dataset.directUploadUrl || '/api/v1/uploads/direct-upload/';
  var ACTIVITY_ID = wizard.dataset.activityId || '';
  var UPLOAD_LOCKED = wizard.dataset.uploadLocked === 'true';
  var CHUNK_SIZE = 5 * 1024 * 1024;
  var uploadJobs = {};
  var uploadSeq = 0;

  var MIME_BY_EXT = {
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.pdf': 'application/pdf',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.mp4': 'video/mp4',
    '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo',
    '.mkv': 'video/x-matroska',
    '.zip': 'application/zip'
  };

  function readJson(id, fallback) {
    var node = document.getElementById(id);
    if (!node) {
      return fallback;
    }
    try {
      return JSON.parse(node.textContent || '[]');
    } catch (err) {
      return fallback;
    }
  }

  function extOf(name) {
    var lower = String(name || '').toLowerCase();
    var dot = lower.lastIndexOf('.');
    return dot >= 0 ? lower.slice(dot) : '';
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  /* ------------------------------------------------------------------
   * 步骤条与面板切换
   * ------------------------------------------------------------------ */
  var TOTAL_STEPS = 4;
  var currentStep = 1;
  var panels = Array.prototype.slice.call(document.querySelectorAll('.wizard-panel'));
  var stepLinks = Array.prototype.slice.call(document.querySelectorAll('#wizard-steps .step-item'));
  var btnPrev = document.getElementById('btn-prev');
  var btnNext = document.getElementById('btn-next');
  var stepIndicator = document.getElementById('step-indicator');

  function showStep(step, options) {
    var target = Math.min(Math.max(parseInt(step, 10) || 1, 1), TOTAL_STEPS);
    if (!(options && options.skipValidation) && target > currentStep && !validateStep(currentStep)) {
      return false;
    }
    currentStep = target;
    panels.forEach(function (panel) {
      var isActive = String(panel.dataset.step) === String(target);
      panel.classList.toggle('d-none', !isActive);
    });
    stepLinks.forEach(function (link) {
      var index = parseInt(link.dataset.stepTarget, 10);
      link.classList.toggle('active', index === target);
      link.classList.toggle('done', index < target);
      if (index === target) {
        link.setAttribute('aria-current', 'step');
      } else {
        link.removeAttribute('aria-current');
      }
    });
    btnPrev.disabled = target === 1;
    btnNext.classList.toggle('d-none', target === TOTAL_STEPS);
    if (stepIndicator) {
      stepIndicator.textContent = '第 ' + target + ' / ' + TOTAL_STEPS + ' 步';
    }
    if (target === 4) {
      renderSummary();
    }
    return true;
  }

  stepLinks.forEach(function (link) {
    link.addEventListener('click', function (event) {
      event.preventDefault();
      showStep(link.dataset.stepTarget, { skipValidation: true });
    });
  });

  btnPrev.addEventListener('click', function () {
    showStep(currentStep - 1, { skipValidation: true });
  });

  btnNext.addEventListener('click', function () {
    showStep(currentStep + 1);
  });

  /* ------------------------------------------------------------------
   * Step 1 校验
   * ------------------------------------------------------------------ */
  var fieldProjectName = document.getElementById('field-project-name');
  var fieldAdvisor = document.getElementById('field-advisor');
  var fieldPlace = document.getElementById('field-place');
  var fieldDateStart = document.getElementById('field-date-start');
  var fieldDateEnd = document.getElementById('field-date-end');
  var fieldIntro = document.getElementById('field-intro');
  var dateError = document.getElementById('error-date');
  var dateSpanHint = document.getElementById('date-span-hint');

  function flag(input, valid) {
    if (!input) {
      return;
    }
    input.classList.toggle('is-invalid', !valid);
    input.classList.toggle('is-valid', valid && String(input.value || '').trim() !== '');
  }

  function validateStepOne() {
    var ok = true;
    [fieldProjectName, fieldAdvisor, fieldPlace].forEach(function (input) {
      var valid = input && String(input.value || '').trim().length > 0;
      flag(input, valid);
      ok = ok && valid;
    });
    var start = fieldDateStart.value;
    var end = fieldDateEnd.value;
    var datesValid = Boolean(start && end && end >= start);
    fieldDateStart.classList.toggle('is-invalid', !datesValid);
    fieldDateEnd.classList.toggle('is-invalid', !datesValid);
    if (dateError) {
      dateError.classList.toggle('d-none', datesValid);
    }
    return ok && datesValid;
  }

  function refreshDateSpan() {
    if (!dateSpanHint) {
      return;
    }
    var start = fieldDateStart.value;
    var end = fieldDateEnd.value;
    if (start && end && end >= start) {
      var days = Math.round((new Date(end) - new Date(start)) / 86400000) + 1;
      dateSpanHint.textContent = '共 ' + days + ' 天';
    } else {
      dateSpanHint.textContent = '';
    }
  }
  fieldDateStart.addEventListener('change', refreshDateSpan);
  fieldDateEnd.addEventListener('change', refreshDateSpan);

  /* ------------------------------------------------------------------
   * Step 2 申报模式与队员行
   * ------------------------------------------------------------------ */
  var radioIndividual = document.getElementById('radio_decl_individual');
  var radioTeam = document.getElementById('radio_decl_team');
  var individualNotice = document.getElementById('individual-notice');
  var stepTwoBody = document.getElementById('step-2-body');
  var teamBlock = document.getElementById('team-block');
  var membersBox = document.getElementById('team-members');
  var memberTemplate = document.getElementById('team-member-template');
  var btnAddMember = document.getElementById('btn-add-member');

  function isTeamMode() {
    return Boolean(radioTeam && radioTeam.checked);
  }

  function applyDeclarationMode() {
    var team = isTeamMode();
    // 个人申报：Step 2 主体整块灰掉（仅保留顶部模式单选可操作）并显示提示
    if (stepTwoBody) {
      stepTwoBody.classList.toggle('wizard-step-muted', !team);
    }
    individualNotice.classList.toggle('d-none', team);
    teamBlock.classList.toggle('d-none', !team);
    if (team && !membersBox.children.length) {
      addMemberRow();
    }
  }

  function addMemberRow(preset) {
    if (!memberTemplate) {
      return null;
    }
    var fragment = memberTemplate.content.cloneNode(true);
    var row = fragment.querySelector('.team-member-row');
    var numberInput = row.querySelector('.member-student-number');
    var nameInput = row.querySelector('.member-real-name');
    var errorHint = row.querySelector('.member-lookup-error');
    var removeBtn = row.querySelector('.member-remove');

    if (preset) {
      numberInput.value = preset.student_number || '';
      nameInput.value = preset.real_name || '';
    }

    numberInput.addEventListener('blur', function () {
      lookupStudent(numberInput, nameInput, errorHint);
    });
    numberInput.addEventListener('input', function () {
      nameInput.value = '';
      errorHint.classList.add('d-none');
      numberInput.classList.remove('is-invalid');
    });
    removeBtn.addEventListener('click', function () {
      row.remove();
      if (!membersBox.children.length && isTeamMode()) {
        addMemberRow();
      }
    });

    membersBox.appendChild(fragment);
    return numberInput;
  }

  var lookupCache = {};

  function lookupStudent(numberInput, nameInput, errorHint) {
    var studentNumber = String(numberInput.value || '').trim();
    if (!studentNumber) {
      nameInput.value = '';
      errorHint.classList.add('d-none');
      return;
    }
    numberInput.classList.remove('is-invalid');
    errorHint.classList.add('d-none');

    if (lookupCache[studentNumber]) {
      nameInput.value = lookupCache[studentNumber];
      return;
    }

    var url = LOOKUP_URL + '?student_number=' + encodeURIComponent(studentNumber);
    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' })
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error('HTTP ' + resp.status);
        }
        return resp.json();
      })
      .then(function (data) {
        if (data && data.exists && data.real_name) {
          nameInput.value = data.real_name;
          lookupCache[studentNumber] = data.real_name;
        } else {
          nameInput.value = '';
          numberInput.classList.add('is-invalid');
          errorHint.classList.remove('d-none');
        }
      })
      .catch(function () {
        nameInput.value = '';
        numberInput.classList.add('is-invalid');
        errorHint.textContent = '学号查询失败，请稍后重试或联系管理员。';
        errorHint.classList.remove('d-none');
      });
  }

  if (btnAddMember) {
    btnAddMember.addEventListener('click', function () {
      var input = addMemberRow();
      if (input) {
        input.focus();
      }
    });
  }
  radioIndividual.addEventListener('change', applyDeclarationMode);
  radioTeam.addEventListener('change', applyDeclarationMode);

  if (Array.isArray(INITIAL_MEMBERS) && INITIAL_MEMBERS.length) {
    INITIAL_MEMBERS.forEach(function (member) {
      addMemberRow(member);
    });
  }

  function collectMembers() {
    return Array.prototype.slice.call(membersBox.querySelectorAll('.team-member-row')).map(function (row) {
      return {
        student_number: String(row.querySelector('.member-student-number').value || '').trim(),
        real_name: String(row.querySelector('.member-real-name').value || '').trim()
      };
    }).filter(function (member) {
      return member.student_number || member.real_name;
    });
  }

  function validateStepTwo() {
    if (!isTeamMode()) {
      return true;
    }
    var members = collectMembers();
    var invalid = members.length === 0;
    Array.prototype.forEach.call(membersBox.querySelectorAll('.team-member-row'), function (row) {
      var numberInput = row.querySelector('.member-student-number');
      var nameInput = row.querySelector('.member-real-name');
      var number = String(numberInput.value || '').trim();
      var name = String(nameInput.value || '').trim();
      if (!number) {
        invalid = true;
        numberInput.classList.add('is-invalid');
      }
      if (!name) {
        invalid = true;
        row.querySelector('.member-lookup-error').classList.remove('d-none');
      }
    });
    return !invalid;
  }

  /* ------------------------------------------------------------------
   * Step 3 FilePond 实例
   * ------------------------------------------------------------------ */
  var ponds = {};

  function registerFilePondPlugins() {
    if (!window.FilePond) {
      return false;
    }
    var plugins = [
      window.FilePondPluginFileValidateType,
      window.FilePondPluginFileValidateSize,
      window.FilePondPluginImageValidateSize,
      window.FilePondPluginImagePreview,
      window.FilePondPluginImageExifOrientation,
      window.FilePondPluginFilePoster,
      window.FilePondPluginFileMetadata
    ].filter(Boolean);
    if (plugins.length) {
      window.FilePond.registerPlugin.apply(window.FilePond, plugins);
    }
    // 插件注册之后才能设置插件自带的文案项（labelMaxFileSize 等），
    // 否则语言包里的键在插件选项尚未注册时会被丢弃，报错文案会退回英文默认值。
    if (window.FilePondLocaleZhCN) {
      window.FilePond.setOptions(window.FilePondLocaleZhCN);
    }
    return true;
  }

  function formatMegabytes(value) {
    var n = Number(value) || 0;
    if (n >= 1) {
      return (Math.round(n * 10) / 10) + 'MB';
    }
    return (Math.round(n * 1000) / 1000) + 'MB';
  }

  // 份数上限按扩展名分档：例如 other_materials 里 .zip 最多 5 份，
  // 其余扩展名仍走类别 max_count（10 份）。
  function extensionCountCap(config, filename) {
    var ext = extOf(filename);
    var limits = config.extension_count_limits || [];
    for (var i = 0; i < limits.length; i++) {
      if (String(limits[i].ext || '').toLowerCase() === ext) {
        return Number(limits[i].max_count) || 0;
      }
    }
    return 0;
  }

  function currentExtensionCount(code, filename) {
    var pond = ponds[code];
    if (!pond) {
      return 0;
    }
    var ext = extOf(filename);
    return pond.getFiles().filter(function (item) {
      return extOf(item.file && item.file.name) === ext;
    }).length;
  }

  // 单份上限按扩展名分档：例如 other_materials 里 .zip 允许 2000MB，
  // 其余扩展名仍走类别上限（50MB）。
  function perFileLimitMb(config, filename) {
    var ext = extOf(filename);
    var limits = config.extension_limits || [];
    for (var i = 0; i < limits.length; i++) {
      if (String(limits[i].ext || '').toLowerCase() === ext) {
        return Number(limits[i].max_mb) || config.max_file_mb;
      }
    }
    return config.max_file_mb;
  }

  function buildZoneOptions(config) {
    var accepted = [];
    var labelMap = {};
    var labelList = [];
    (config.allowed_extensions || []).forEach(function (ext) {
      var mime = MIME_BY_EXT[ext] || 'application/octet-stream';
      var label = ext.replace('.', '').toUpperCase();
      if (accepted.indexOf(mime) === -1) {
        accepted.push(mime);
      }
      labelMap[mime] = label;
      if (labelList.indexOf(label) === -1) {
        labelList.push(label);
      }
    });

    var options = {
      name: config.code,
      allowMultiple: config.max_count > 1,
      maxFiles: config.max_count,
      acceptedFileTypes: accepted,
      fileValidateTypeLabelExpectedTypesMap: labelMap,
      fileValidateTypeLabelExpectedTypes: '仅支持 ' + labelList.join(' / ') + ' 格式',
      fileValidateTypeDetectType: function (source, type) {
        return new Promise(function (resolve) {
          var ext = extOf(source && source.name);
          resolve(MIME_BY_EXT[ext] || type);
        });
      },
      // FilePond 只支持一个全局上限，取该区最宽的一档（含 ZIP 2000MB），
      // 分档的精确拦截放在 beforeAddFile 里做。
      maxFileSize: (config.max_file_mb_widest || config.max_file_mb) + 'MB',
      // beforeAddFile 拿到的是 FilePond item（含 getMetadata/setFile 等方法），
      // 真正的 File 对象在 item.file 上，name/size 都要从那里取。
      beforeAddFile: function (item) {
        var file = (item && item.file) || item || {};
        var countCap = extensionCountCap(config, file.name);
        if (countCap && currentExtensionCount(config.code, file.name) + 1 > countCap) {
          window.alert(
            '「' + config.title + '」中 ' + extOf(file.name).replace('.', '').toUpperCase() +
            ' 文件最多 ' + countCap + ' 份，请先移除已上传的文件。'
          );
          return false;
        }
        if (!config.extension_limits || !config.extension_limits.length) {
          return true;
        }
        var limitMb = perFileLimitMb(config, file.name);
        if (file.size > limitMb * 1024 * 1024) {
          window.alert(
            '「' + config.title + '」单份不得超过 ' + formatMegabytes(limitMb) +
            '，当前文件 ' + formatMegabytes(file.size / 1048576) + '。'
          );
          return false;
        }
        return true;
      },
      allowImagePreview: Boolean(config.allow_image_preview),
      allowFilePoster: true,
      imagePreviewMaxHeight: 200,
      allowImageValidateSize: Boolean(config.is_image_zone),
      imageValidateSizeMinWidth: 200,
      imageValidateSizeMinHeight: 200,
      // 选完文件立即上传：视频每一片 5MB 单独成请求，页面顶部进度条实时汇总。
      instantUpload: !UPLOAD_LOCKED,
      disabled: UPLOAD_LOCKED,
      server: { process: makeUploadProcess(config) },
      labelIdle: FilePondLocaleIdle(config)
    };
    return options;
  }

  function FilePondLocaleIdle(config) {
    var hint = config.required
      ? config.count_hint
      : '选交，最多 ' + config.max_count + ' 份';
    return '拖放' + escapeHtml(config.title) + '，或者 <span class="filepond--label-action">浏览</span>' +
      '<br><span class="filepond--label-hint">' + escapeHtml(hint) + ' · ' + escapeHtml(config.limit_label || ('单份 ≤ ' + config.max_file_mb + 'MB')) + '</span>';
  }

  /* ------------------------------------------------------------------
   * 上传通道：宣讲视频 -> chunkStart / chunkUpload x N / chunkComplete
   *           其余材料 -> 单请求 direct-upload
   * 两条通道共用顶部 Tabler 进度条与「上传中勿关闭页面」提醒。
   * ------------------------------------------------------------------ */
  function renderUploadProgress() {
    var wrap = document.getElementById('upload-progress-bar');
    if (!wrap) {
      return;
    }
    var fill = document.getElementById('upload-progress-fill');
    var label = document.getElementById('upload-progress-label');
    var percentNode = document.getElementById('upload-progress-percent');
    var track = document.getElementById('upload-progress-track');
    var hint = document.getElementById('upload-progress-hint');
    var ids = Object.keys(uploadJobs);
    if (!ids.length) {
      wrap.classList.add('d-none');
      if (fill) { fill.style.width = '0%'; }
      if (percentNode) { percentNode.textContent = '0%'; }
      if (track) { track.setAttribute('aria-valuenow', '0'); }
      if (hint) { hint.classList.add('d-none'); }
      return;
    }
    var loaded = 0;
    var total = 0;
    ids.forEach(function (id) {
      loaded += uploadJobs[id].loaded;
      total += uploadJobs[id].total;
    });
    var percent = Math.max(0, Math.min(100, Math.round((loaded / Math.max(1, total)) * 100)));
    wrap.classList.remove('d-none');
    if (fill) { fill.style.width = percent + '%'; }
    if (percentNode) { percentNode.textContent = percent + '%'; }
    if (track) { track.setAttribute('aria-valuenow', String(percent)); }
    if (label) {
      label.textContent = '正在上传 ' + ids.length + ' 个文件（' + formatBytes(loaded) + ' / ' + formatBytes(total) + '）';
    }
    if (hint) { hint.classList.remove('d-none'); }
  }

  function startUploadJob(totalBytes) {
    uploadSeq += 1;
    var job = { id: 'upload-' + uploadSeq, loaded: 0, total: Math.max(1, Number(totalBytes) || 1) };
    uploadJobs[job.id] = job;
    renderUploadProgress();
    return job;
  }

  function updateUploadJob(job, loaded) {
    if (!job) {
      return;
    }
    job.loaded = Math.max(0, Math.min(Number(loaded) || 0, job.total));
    uploadJobs[job.id] = job;
    renderUploadProgress();
  }

  function finishUploadJob(job) {
    if (!job) {
      return;
    }
    delete uploadJobs[job.id];
    renderUploadProgress();
  }

  // 上传过程中关闭/刷新页面给出原生确认框
  window.addEventListener('beforeunload', function (event) {
    if (!Object.keys(uploadJobs).length) {
      return undefined;
    }
    event.preventDefault();
    event.returnValue = '附件仍在上传中，离开页面会中断上传。';
    return event.returnValue;
  });

  function postForm(url, fields, blob, onProgress) {
    var xhr = new XMLHttpRequest();
    var aborted = false;
    var promise = new Promise(function (resolve, reject) {
      var form = new FormData();
      Object.keys(fields || {}).forEach(function (key) {
        var value = fields[key];
        if (value !== undefined && value !== null && value !== '') {
          form.append(key, value);
        }
      });
      if (blob) {
        form.append(blob.field || 'chunk', blob.value, blob.name || 'blob');
      }
      xhr.open('POST', url, true);
      xhr.withCredentials = true;
      xhr.setRequestHeader('X-CSRFToken', CSRF_TOKEN);
      xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
      if (onProgress) {
        xhr.upload.onprogress = function (evt) {
          if (evt.lengthComputable) {
            onProgress(evt.loaded, evt.total);
          }
        };
      }
      xhr.onload = function () {
        var payload = null;
        try {
          payload = JSON.parse(xhr.responseText || '{}');
        } catch (err) {
          payload = null;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve({ status: xhr.status, data: payload || {} });
          return;
        }
        var detail = payload && payload.error && payload.error.message;
        var failure = new Error(detail || ('上传失败（HTTP ' + xhr.status + '）'));
        failure.status = xhr.status;
        failure.payload = payload;
        reject(failure);
      };
      xhr.onerror = function () {
        reject(new Error('网络中断，上传未完成。'));
      };
      xhr.onabort = function () {
        aborted = true;
        var failure = new Error('上传已取消。');
        failure.aborted = true;
        reject(failure);
      };
      xhr.send(form);
    });
    return {
      promise: promise,
      abort: function () {
        if (!aborted) {
          xhr.abort();
        }
      }
    };
  }

  // 只对网络错误 / 5xx 重试；4xx 是业务拒绝，重试没有意义。
  function retrying(taskFactory, attempts) {
    var remaining = attempts || 3;
    function run() {
      return taskFactory().catch(function (err) {
        var permanent = err && (err.aborted || (err.status && err.status >= 400 && err.status < 500));
        if (permanent || remaining <= 1) {
          throw err;
        }
        remaining -= 1;
        return new Promise(function (resolve) {
          window.setTimeout(resolve, 800);
        }).then(run);
      });
    }
    return run();
  }

  function makeUploadProcess(config) {
    return function (fieldName, file, metadata, load, error, progress, abort) {
      if (config && config.code === 'video') {
        return chunkedUpload(config, file, load, error, progress);
      }
      return directUpload(config, file, load, error, progress);
    };
  }

  function directUpload(config, file, load, error, progress) {
    var job = startUploadJob(file.size);
    var request = postForm(DIRECT_UPLOAD_URL, { requirement_id: config.requirement_id }, {
      field: 'file',
      value: file,
      name: file.name
    }, function (loaded, total) {
      updateUploadJob(job, loaded);
      if (progress) {
        progress(true, loaded, total);
      }
    });
    request.promise
      .then(function (res) {
        if (progress) {
          progress(true, file.size, file.size);
        }
        finishUploadJob(job);
        load(String((res.data && res.data.upload_id) || (res.data && res.data.id) || file.name));
      })
      .catch(function (err) {
        finishUploadJob(job);
        error(err && err.message ? err.message : '上传失败，请重试。');
      });
    return { abort: function () { request.abort(); } };
  }

  function chunkedUpload(config, file, load, error, progress) {
    var job = startUploadJob(file.size);
    var controllers = [];
    var stopped = false;
    var uploadId = '';
    var chunkSize = CHUNK_SIZE;
    var received = {};
    var uploadedBytes = 0;

    function cancel() {
      stopped = true;
      controllers.forEach(function (item) {
        try {
          item.abort();
        } catch (err) {
          /* 已经结束的请求忽略 */
        }
      });
    }

    function report(bytes) {
      uploadedBytes += Math.max(0, bytes);
      updateUploadJob(job, uploadedBytes);
      if (progress) {
        progress(true, uploadedBytes, file.size);
      }
    }

    function sendPart(index) {
      var start = index * chunkSize;
      var end = Math.min(start + chunkSize, file.size);
      var request = postForm(CHUNK_UPLOAD_URL, { upload_id: uploadId, chunk_index: index }, {
        field: 'chunk',
        value: file.slice(start, end),
        name: 'part-' + index
      });
      controllers.push(request);
      return request.promise.then(function () {
        report(end - start);
      });
    }

    function uploadAll(totalChunks) {
      var index = 0;
      function next() {
        if (stopped) {
          return Promise.reject(new Error('上传已取消。'));
        }
        if (index >= totalChunks) {
          return Promise.resolve();
        }
        var current = index;
        index += 1;
        var start = current * chunkSize;
        var end = Math.min(start + chunkSize, file.size);
        if (received[current]) {
          // 服务端已有这一片（断点续传），直接跳过，不再浪费上行带宽
          report(end - start);
          return next();
        }
        return retrying(function () { return sendPart(current); }).then(next);
      }
      return next();
    }

    postForm(CHUNK_START_URL, {
      material_type: config.code,
      filename: file.name,
      file_size: file.size,
      total_chunks: Math.max(1, Math.ceil(file.size / CHUNK_SIZE)),
      requirement_id: config.requirement_id,
      activity_id: ACTIVITY_ID
    }).promise
      .then(function (res) {
        var data = (res && res.data) || {};
        uploadId = data.upload_id;
        if (!uploadId) {
          throw new Error('服务端未返回 upload_id，无法继续分片上传。');
        }
        chunkSize = Number(data.chunk_size) || CHUNK_SIZE;
        received = {};
        (data.received_chunks || []).forEach(function (idx) {
          received[idx] = true;
        });
        var totalChunks = Number(data.total_chunks) || Math.max(1, Math.ceil(file.size / chunkSize));
        return uploadAll(totalChunks);
      })
      .then(function () {
        return retrying(function () {
          var request = postForm(CHUNK_COMPLETE_URL, { upload_id: uploadId, material_type: config.code });
          controllers.push(request);
          return request.promise;
        }, 2);
      })
      .then(function (res) {
        // FilePond 要求 load 之前把进度推到 100%，否则条目会停在半程状态
        if (progress) {
          progress(true, file.size, file.size);
        }
        finishUploadJob(job);
        load(String((res && res.data && res.data.upload_id) || uploadId));
      })
      .catch(function (err) {
        finishUploadJob(job);
        error(err && err.message ? err.message : '分片上传失败，请重试。');
      });

    return { abort: cancel };
  }

  function initFilePonds() {
    if (!registerFilePondPlugins()) {
      window.console && window.console.warn('FilePond 未加载，附件区退化为原生文件输入。');
      return;
    }
    MATERIALS.forEach(function (config) {
      var input = document.getElementById('pond-' + config.code);
      if (!input) {
        return;
      }
      ponds[config.code] = window.FilePond.create(input, buildZoneOptions(config));
    });
  }

  function filesOf(code) {
    var pond = ponds[code];
    if (!pond) {
      return [];
    }
    return pond.getFiles().map(function (item) {
      var native = item.file || null;
      return {
        name: item.filename || (native && native.name) || '未命名文件',
        size: (native && native.size) || item.fileSize || 0,
        native: native,
        ext: extOf(item.filename || (native && native.name))
      };
    });
  }

  function countOf(code) {
    return ponds[code] ? ponds[code].getFiles().length : 0;
  }

  function materialConfig(code) {
    for (var i = 0; i < MATERIALS.length; i += 1) {
      if (MATERIALS[i].code === code) {
        return MATERIALS[i];
      }
    }
    return null;
  }

  function validateStepThree() {
    var problems = [];
    MATERIALS.forEach(function (config) {
      var count = countOf(config.code);
      if (config.required && count < config.min_count) {
        problems.push(config.title + '至少需要 ' + config.min_count + ' 份（当前 ' + count + ' 份）。');
      }
      if (count > config.max_count) {
        problems.push(config.title + '最多 ' + config.max_count + ' 份（当前 ' + count + ' 份）。');
      }
    });
    if (problems.length) {
      window.alert('材料还不齐：\n· ' + problems.join('\n· '));
      return false;
    }
    return true;
  }

  function validateStep(step) {
    if (step === 1) {
      return validateStepOne();
    }
    if (step === 2) {
      return validateStepTwo();
    }
    if (step === 3) {
      return validateStepThree();
    }
    return true;
  }

  /* ------------------------------------------------------------------
   * Step 4 回显与提交
   * ------------------------------------------------------------------ */
  function formatBytes(bytes) {
    if (!bytes) {
      return '0 B';
    }
    if (bytes >= 1048576) {
      return (bytes / 1048576).toFixed(2) + ' MB';
    }
    if (bytes >= 1024) {
      return (bytes / 1024).toFixed(1) + ' KB';
    }
    return bytes + ' B';
  }

  function setSummary(key, value) {
    var node = document.querySelector('[data-summary="' + key + '"]');
    if (node) {
      node.textContent = value || '—';
    }
  }

  function renderSummary() {
    setSummary('project_name', fieldProjectName.value.trim());
    setSummary('advisor_name', fieldAdvisor.value.trim());
    setSummary('practice_place', fieldPlace.value.trim());
    setSummary('date_range', fieldDateStart.value && fieldDateEnd.value ? fieldDateStart.value + ' 至 ' + fieldDateEnd.value : '');
    setSummary('project_intro', fieldIntro.value.trim());

    var teamBox = document.getElementById('summary-team');
    if (teamBox) {
      if (!isTeamMode()) {
        teamBox.innerHTML = '<span class="badge bg-blue-lt">个人申报</span> <span class="text-secondary ms-2">个人申报无需成员。</span>';
      } else {
        var members = collectMembers();
        if (!members.length) {
          teamBox.innerHTML = '<span class="badge bg-blue-lt">团队申报</span> <span class="text-secondary ms-2">尚未添加队员。</span>';
        } else {
          teamBox.innerHTML = '<span class="badge bg-blue-lt">团队申报</span> <div class="mt-2 d-flex flex-wrap gap-2">' +
            members.map(function (member) {
              return '<span class="badge bg-secondary-lt">' + escapeHtml(member.student_number) + ' ' + escapeHtml(member.real_name) + '</span>';
            }).join('') + '</div>';
        }
      }
    }

    var box = document.getElementById('summary-materials');
    if (box) {
      box.innerHTML = '';
      MATERIALS.forEach(function (config) {
        var files = filesOf(config.code);
        var card = document.createElement('div');
        card.className = 'col-12 col-md-6 col-xl-4';
        var body = files.length
          ? files.map(function (file) {
              var isImage = ['.jpg', '.jpeg', '.png'].indexOf(file.ext) !== -1 && file.native;
              var thumb = isImage
                ? '<img src="' + window.URL.createObjectURL(file.native) + '" alt="' + escapeHtml(file.name) + '" class="apply-thumb">'
                : '<span class="avatar avatar-sm bg-secondary-lt">' + escapeHtml(file.ext.replace('.', '').toUpperCase() || '文件') + '</span>';
              return '<div class="d-flex align-items-center gap-2 mb-2">' + thumb +
                '<div class="text-truncate"><div class="text-truncate">' + escapeHtml(file.name) + '</div>' +
                '<div class="text-secondary small">' + formatBytes(file.size) + '</div></div></div>';
            }).join('')
          : '<div class="text-secondary small">未上传</div>';
        card.innerHTML = '<div class="card h-100"><div class="card-body">' +
          '<div class="d-flex justify-content-between align-items-center mb-2">' +
          '<strong>' + escapeHtml(config.title) + '</strong>' +
          (config.required
            ? '<span class="badge bg-red-lt">必交 ' + config.min_count + '～' + config.max_count + '</span>'
            : '<span class="badge bg-secondary-lt">选交</span>') +
          '</div>' + body + '</div></div>';
        box.appendChild(card);
      });
    }
  }

  function showSubmitError(message) {
    var box = document.getElementById('submit-error');
    if (!box) {
      return;
    }
    box.textContent = message;
    box.classList.remove('d-none');
    box.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function hideSubmitError() {
    var box = document.getElementById('submit-error');
    if (box) {
      box.classList.add('d-none');
      box.textContent = '';
    }
  }

  function uuid4() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (ch) {
      var rand = (Math.random() * 16) | 0;
      var value = ch === 'x' ? rand : (rand & 0x3) | 0x8;
      return value.toString(16);
    });
  }

  var btnSubmit = document.getElementById('btn-confirm-submit');

  function submitApplication() {
    if (!CAN_SUBMIT || !SUBMIT_URL) {
      showSubmitError('当前没有可提交的申报记录（请确认活动是否开放，或刷新页面后重试）。');
      return;
    }
    if (!validateStepOne() || !validateStepTwo()) {
      showSubmitError('表单信息不完整，请返回对应步骤补全后再提交。');
      return;
    }
    hideSubmitError();
    btnSubmit.disabled = true;
    btnSubmit.classList.add('btn-loading');

    fetch(SUBMIT_URL, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': CSRF_TOKEN,
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: JSON.stringify({ request_key: uuid4() })
    })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (data) {
          return { ok: resp.ok, status: resp.status, data: data };
        });
      })
      .then(function (result) {
        if (result.ok) {
          window.location.href = SUCCESS_URL || DETAIL_URL;
          return;
        }
        var message = (result.data && result.data.error && result.data.error.message) ||
          ('提交失败（HTTP ' + result.status + '），请稍后重试。');
        showSubmitError(message);
      })
      .catch(function (err) {
        showSubmitError('网络异常，提交未完成：' + (err && err.message ? err.message : '未知错误'));
      })
      .then(function () {
        btnSubmit.disabled = false;
        btnSubmit.classList.remove('btn-loading');
      });
  }

  if (btnSubmit) {
    btnSubmit.addEventListener('click', submitApplication);
  }

  /* ------------------------------------------------------------------
   * 启动
   * ------------------------------------------------------------------ */
  applyDeclarationMode();
  initFilePonds();
  refreshDateSpan();

  // 支持 #step-3 这类深链直接落位（步骤条本身也可点击切换）
  var deepLink = parseInt(String(window.location.hash || '').replace('#step-', ''), 10);
  showStep(deepLink >= 1 && deepLink <= TOTAL_STEPS ? deepLink : 1, { skipValidation: true });
})();