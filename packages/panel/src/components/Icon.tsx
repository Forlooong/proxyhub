export type IconName = 'overview' | 'server' | 'site' | 'proxy' | 'subscription' | 'jobs' | 'settings' | 'arrow' | 'close' | 'check' | 'copy' | 'search' | 'sun' | 'moon' | 'logout' | 'refresh' | 'lock' | 'terminal' | 'external' | 'key';
const paths: Record<IconName, string> = {
  overview: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  server: 'M4 3h16v7H4zM4 14h16v7H4zM7 6h.01M7 17h.01M11 6h6M11 17h6',
  site: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18',
  proxy: 'M4 6h12M12 2l4 4-4 4M20 18H8M12 14l-4 4 4 4M4 12v8M20 4v8',
  subscription: 'M9 15l6-6M8 16H6a4 4 0 0 1-3-7l4-4a4 4 0 0 1 6 0M16 8h2a4 4 0 0 1 3 7l-4 4a4 4 0 0 1-6 0',
  jobs: 'M9 5h11M9 12h11M9 19h11M3 5h.01M3 12h.01M3 19h.01',
  settings: 'M12 8v5M12 17h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  arrow: 'M4 12h16M14 6l6 6-6 6', close: 'M6 6l12 12M18 6L6 18', check: 'M5 12l4 4L19 6',
  copy: 'M8 8h12v13H8zM16 8V3H3v13h5', search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  sun: 'M12 3V1M12 23v-2M3 12H1M23 12h-2M4 4l2 2M18 18l2 2M20 4l-2 2M6 18l-2 2M17 12a5 5 0 1 1-10 0 5 5 0 0 1 10 0',
  moon: 'M20 15A9 9 0 0 1 9 4 9 9 0 1 0 20 15', logout: 'M9 3H3v18h6M9 12h12M17 8l4 4-4 4',
  refresh: 'M20 9a8 8 0 0 0-14-5L3 7M3 2v5h5M4 15a8 8 0 0 0 14 5l3-3M21 22v-5h-5',
  lock: 'M5 10h14v11H5zM8 10V6a4 4 0 0 1 8 0v4M12 14v3',
  terminal: 'M3 4h18v16H3zM6 8l4 4-4 4M13 16h5', external: 'M14 3h7v7M21 3l-9 9M10 3H3v18h18v-7',
  key: 'M14 10a5 5 0 1 1-10 0 5 5 0 0 1 10 0M14 10h8M18 10v4M21 10v3',
};
export function Icon({name, className = ''}: {name: IconName; className?: string}) {
  return <svg className={`icon ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}
