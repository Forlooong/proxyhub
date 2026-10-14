/** Preserve server epochs until the user explicitly edits a field. */
export type TimestampView = {value: string; warning: string; unsafe: boolean};

export function timestampView(seconds: number | null | undefined): TimestampView {
  if (seconds === null || seconds === undefined) return {value: '', warning: '', unsafe: false};
  if (!Number.isSafeInteger(seconds) || seconds < 0) {
    return {value: '', unsafe: true, warning: '原值超出浏览器可精确处理的时间范围。请明确替换或清除此时间，再预览变更；不会自动清空原值。'};
  }
  const value = new Date(seconds * 1000);
  if (!Number.isFinite(value.valueOf()) || value.getFullYear() < 1 || value.getFullYear() > 9999) {
    return {value: '', unsafe: false, warning: `原值 ${seconds} 秒超出日期控件的显示范围。未编辑时会原样保留；也可明确替换或清除。`};
  }
  const pad = (part: number) => String(part).padStart(2, '0');
  return {value: `${String(value.getFullYear()).padStart(4, '0')}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}:${pad(value.getSeconds())}`, warning: '', unsafe: false};
}

export function resolveTimestamp(original: number | null | undefined, value: string, edited: boolean, label: string): number | null {
  if (!edited) {
    if (timestampView(original).unsafe) throw new Error(`${label}原值无法精确处理，请明确替换或清除后再预览。`);
    return original ?? null;
  }
  if (!value) return null;
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) throw new Error(`${label}格式无效，请重新选择。`);
  const milliseconds = new Date(value).valueOf();
  const seconds = milliseconds / 1000;
  if (!Number.isSafeInteger(seconds) || seconds < 0 || timestampView(seconds).value.slice(0, value.length) !== value) {
    throw new Error(`${label}不是有效的本地时间，请重新选择。`);
  }
  return seconds;
}
