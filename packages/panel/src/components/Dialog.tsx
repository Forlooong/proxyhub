import { useEffect, useRef, type ReactNode } from 'react';
import { Icon } from './Icon';

export function Dialog({title, onClose, children, className = '', locked = false}: {title: string; onClose: () => void; children: ReactNode; className?: string; locked?: boolean}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={ref} className={`dialog ${className}`} aria-label={title} onCancel={event => {event.preventDefault(); if (!locked) onClose();}}>
    <div className="dialog-head"><h2>{title}</h2><button className="icon-button" aria-label="关闭" disabled={locked} onClick={onClose}><Icon name="close" /></button></div>
    {children}
  </dialog>;
}
