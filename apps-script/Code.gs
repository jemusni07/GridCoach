/**
 * GridCoach — Google Sheets bound script.
 * Menu + sidebar; every AI call is proxied to the FastAPI backend via UrlFetchApp
 * (no CORS, the API key never reaches the browser).
 *
 * Script Properties (Project Settings → Script Properties):
 *   BACKEND_URL  e.g. https://xxxx.ngrok-free.app  (or http://localhost:8000 won't work — must be reachable from Google)
 *   API_KEY      same value as GRIDCOACH_API_KEY in the backend .env
 */

const PROPS = PropertiesService.getScriptProperties();

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('GridCoach')
    .addItem('Open coach', 'showSidebar')
    .addSeparator()
    .addItem('Choose tabs…', 'showSidebar')
    .addItem('Connect Strava', 'menuConnectStrava')
    .addItem('Sync Strava (90 days)', 'menuSync')
    .addItem('Refresh weather tab', 'menuWeather')
    .addItem('Recompute snapshot', 'menuRecompute')
    .addSeparator()
    .addItem('Configure backend…', 'configureBackend')
    .addToUi();
}

function showSidebar() {
  const html = HtmlService.createHtmlOutputFromFile('Sidebar').setTitle('GridCoach');
  SpreadsheetApp.getUi().showSidebar(html);
}

// ---------- backend plumbing ----------

function backendConfig_() {
  const url = (PROPS.getProperty('BACKEND_URL') || '').replace(/\/+$/, '');
  const key = PROPS.getProperty('API_KEY') || '';
  if (!url || !key) throw new Error('Backend not configured. GridCoach → Configure backend…');
  return { url, key };
}

function api_(method, path, payload) {
  const { url, key } = backendConfig_();
  const opts = {
    method,
    contentType: 'application/json',
    headers: { 'X-API-Key': key },
    muteHttpExceptions: true,
  };
  if (payload && method !== 'get') opts.payload = JSON.stringify(payload);
  const res = UrlFetchApp.fetch(url + path, opts);
  const code = res.getResponseCode();
  let body;
  try { body = JSON.parse(res.getContentText()); } catch (e) { body = { error: res.getContentText().slice(0, 300) }; }
  if (code >= 400) throw new Error(body.error || body.detail || ('Backend error ' + code));
  return body;
}

function spreadsheetId_() {
  return SpreadsheetApp.getActiveSpreadsheet().getId();
}

/** What the model sees about where the athlete is in the sheet right now. */
function getSheetContext() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getActiveSheet();
  const range = ss.getActiveRange();
  let selection = null;
  if (range) {
    const maxRows = 50, maxCols = 20;
    const nRows = Math.min(range.getNumRows(), maxRows);
    const nCols = Math.min(range.getNumColumns(), maxCols);
    const values = sheet.getRange(range.getRow(), range.getColumn(), nRows, nCols).getDisplayValues();
    selection = {
      a1: range.getA1Notation(),
      values,
      truncated: range.getNumRows() > maxRows || range.getNumColumns() > maxCols,
    };
  }
  return {
    spreadsheet_id: ss.getId(),
    spreadsheet_name: ss.getName(),
    active_sheet: sheet.getName(),
    sheet_names: ss.getSheets().map(s => s.getName()),
    selection,
    timezone: ss.getSpreadsheetTimeZone(),
  };
}

// ---------- functions the sidebar calls via google.script.run ----------

/** Starts a chat turn as a background job → { job_id }. The sidebar polls pollJob() for progress + the reply. */
function sendChat(message, reset) {
  return api_('post', '/api/chat', {
    spreadsheet_id: spreadsheetId_(),
    message,
    reset: !!reset,
    background: true,
    context: getSheetContext(),
  });
}

/** Transcript replay + any job still running, so a reopened sidebar resumes where it left off. */
function getHistory() {
  return api_('get', '/api/history?spreadsheet_id=' + encodeURIComponent(spreadsheetId_()) + '&limit=60');
}

function pollJob(jobId, after) {
  return api_('get', '/api/jobs/' + encodeURIComponent(jobId) + '?after=' + (after || 0));
}

/** Athlete pressed Confirm / Cancel on a parked destructive action → { job_id }. */
function resolveAction(actionId, approve) {
  return api_('post', '/api/actions/resolve', { spreadsheet_id: spreadsheetId_(), action_id: actionId, approve: !!approve });
}

function getStatus() {
  const status = api_('get', '/api/status?spreadsheet_id=' + encodeURIComponent(spreadsheetId_()));
  status.sheet_names = SpreadsheetApp.getActiveSpreadsheet().getSheets().map(s => s.getName());
  return status;
}

function resetChat() {
  return api_('post', '/api/chat/reset', { spreadsheet_id: spreadsheetId_() });
}

/** Catalog of optional GridCoach tabs, plus which ones this sheet already has. */
function getTabCatalog() {
  const catalog = api_('get', '/api/tabs').tabs;
  const present = new Set(SpreadsheetApp.getActiveSpreadsheet().getSheets().map(s => s.getName()));
  return catalog.map(t => Object.assign({ exists: present.has(t.name) }, t));
}

/** Create only the tabs the athlete ticked. Tabs are opt-in. */
function setupSheet(tabs) {
  const res = api_('post', '/api/setup', { spreadsheet_id: spreadsheetId_(), tabs: tabs || [] });
  SpreadsheetApp.flush();
  return res;
}

function stravaConnectUrl() {
  return api_('post', '/api/strava/connect-url', { spreadsheet_id: spreadsheetId_() }).url;
}

function syncStrava(daysBack) {
  const { url, key } = backendConfig_();
  const res = UrlFetchApp.fetch(url + '/api/strava/sync?background=true', {
    method: 'post', contentType: 'application/json', headers: { 'X-API-Key': key }, muteHttpExceptions: true,
    payload: JSON.stringify({ spreadsheet_id: spreadsheetId_(), days_back: daysBack || null }),
  });
  let body; try { body = JSON.parse(res.getContentText()); } catch (e) { body = { error: res.getContentText().slice(0, 300) }; }
  if (res.getResponseCode() >= 400) {
    if (body.code === 'missing_tab') return { missing_tab: body.tab, error: body.error };  // let the sidebar offer a fix
    throw new Error(body.error || body.detail || ('Backend error ' + res.getResponseCode()));
  }
  return body;  // { job_id } — the sidebar polls it and highlights touched ranges when done
}

function refreshWeather() {
  const res = api_('post', '/api/weather/refresh', { spreadsheet_id: spreadsheetId_(), days: 7 });
  highlightRanges([res.sheet + '!' + res.range]);
  return res;
}

function recompute() {
  return api_('post', '/api/recompute', { spreadsheet_id: spreadsheetId_() });
}

/** Jump to the first range the agent touched so the athlete sees the change. */
function highlightRanges(ranges) {
  if (!ranges || !ranges.length) return;
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const first = ranges[0];
  const bang = first.lastIndexOf('!');
  const sheetName = bang > 0 ? first.slice(0, bang) : null;
  const a1 = bang > 0 ? first.slice(bang + 1) : first;
  const sheet = sheetName ? ss.getSheetByName(sheetName) : ss.getActiveSheet();
  if (!sheet) return;
  try {
    const range = sheet.getRange(a1);
    ss.setActiveSheet(sheet);
    sheet.setActiveRange(range);
  } catch (e) { /* ignore malformed ranges */ }
}

// ---------- menu handlers ----------

function menuConnectStrava() {
  const ui = SpreadsheetApp.getUi();
  try {
    const url = stravaConnectUrl();
    const html = HtmlService.createHtmlOutput(
      '<div style="font-family:system-ui;padding:8px">' +
      '<p>Authorize GridCoach to read your Strava activities:</p>' +
      '<p><a href="' + url + '" target="_blank" style="font-size:16px">Connect Strava →</a></p>' +
      '<p style="color:#666;font-size:12px">Then come back and run <b>Sync Strava</b>.</p></div>'
    ).setWidth(360).setHeight(160);
    ui.showModalDialog(html, 'Connect Strava');
  } catch (e) { ui.alert('GridCoach', e.message, ui.ButtonSet.OK); }
}

function menuSync() {
  const ui = SpreadsheetApp.getUi();
  try {
    const r = syncStrava(90);
    ui.alert('GridCoach', 'Synced ' + r.fetched + ' activities (' + r.appended + ' new, ' + r.updated + ' updated).', ui.ButtonSet.OK);
  } catch (e) { ui.alert('GridCoach', e.message, ui.ButtonSet.OK); }
}

function menuWeather() {
  const ui = SpreadsheetApp.getUi();
  try { const r = refreshWeather(); ui.alert('GridCoach', 'Weather tab updated (' + r.days + ' days).', ui.ButtonSet.OK); }
  catch (e) { ui.alert('GridCoach', e.message, ui.ButtonSet.OK); }
}

function menuRecompute() {
  const ui = SpreadsheetApp.getUi();
  try { recompute(); ui.alert('GridCoach', 'Health Snapshot and plan statuses recomputed.', ui.ButtonSet.OK); }
  catch (e) { ui.alert('GridCoach', e.message, ui.ButtonSet.OK); }
}

function configureBackend() {
  const ui = SpreadsheetApp.getUi();
  const u = ui.prompt('GridCoach backend', 'Backend URL (e.g. https://xxxx.ngrok-free.app):', ui.ButtonSet.OK_CANCEL);
  if (u.getSelectedButton() !== ui.Button.OK) return;
  const k = ui.prompt('GridCoach backend', 'API key (GRIDCOACH_API_KEY from the backend .env):', ui.ButtonSet.OK_CANCEL);
  if (k.getSelectedButton() !== ui.Button.OK) return;
  PROPS.setProperty('BACKEND_URL', u.getResponseText().trim());
  PROPS.setProperty('API_KEY', k.getResponseText().trim());
  ui.alert('GridCoach', 'Saved. Open the coach from the GridCoach menu.', ui.ButtonSet.OK);
}
