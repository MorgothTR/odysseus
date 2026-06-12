function _apiBase() {
  return window.API_BASE || '';
}

function _tauriInvoke() {
  return window.__TAURI__?.core?.invoke || window.__TAURI__?.invoke || null;
}

function _openPopupOrFallback(url) {
  let opened = null;
  try {
    opened = window.open(url, '_blank', 'noopener,noreferrer');
  } catch (err) {
    console.warn('Could not open research report popup:', err);
  }
  if (!opened) {
    window.location.assign(url);
  }
}

export function researchReportUrl(sessionId) {
  return `${_apiBase()}/api/research/report/${encodeURIComponent(String(sessionId || '').trim())}`;
}

export async function openResearchReport(sessionId) {
  const id = String(sessionId || '').trim();
  if (!id) return;

  const invoke = _tauriInvoke();
  if (invoke) {
    try {
      await invoke('open_research_report', { session_id: id });
      return;
    } catch (err) {
      console.warn('Desktop research report window failed; using browser fallback:', err);
    }
  }

  _openPopupOrFallback(researchReportUrl(id));
}
