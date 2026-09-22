import type { SVGProps } from 'react'

type Props = SVGProps<SVGSVGElement> & { size?: number }

const paths: Record<string, React.ReactNode> = {
  check: <><path d="M20 6 9 17l-5-5"/></>,
  x: <><path d="M18 6 6 18M6 6l12 12"/></>,
  arrow: <><path d="M5 12h14M13 6l6 6-6 6"/></>,
  chevron: <><path d="m9 18 6-6-6-6"/></>,
  play: <><path d="m8 5 11 7-11 7Z"/></>,
  pause: <><path d="M8 5v14M16 5v14"/></>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 9a7 7 0 0 1 11.4-2L20 12M4 12l2.5 5a7 7 0 0 0 11.4-2"/></>,
  shield: <><path d="M12 3 5 6v5c0 4.6 2.8 8 7 10 4.2-2 7-5.4 7-10V6Z"/><path d="m9 12 2 2 4-4"/></>,
  alert: <><path d="M12 3 2.8 20h18.4Z"/><path d="M12 9v4M12 17h.01"/></>,
  file: <><path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></>,
  chart: <><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></>,
  bot: <><rect x="4" y="7" width="16" height="12" rx="3"/><path d="M12 3v4M8 12h.01M16 12h.01M8 16h8"/></>,
  database: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></>,
  link: <><path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1.1M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1.1"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  lock: <><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></>,
  upload: <><path d="M12 16V4M7 9l5-5 5 5M4 20h16"/></>,
  activity: <><path d="M3 12h4l2-6 4 12 2-6h6"/></>,
  sparkles: <><path d="m12 3 1.2 3.8L17 8l-3.8 1.2L12 13l-1.2-3.8L7 8l3.8-1.2ZM5 15l.8 2.2L8 18l-2.2.8L5 21l-.8-2.2L2 18l2.2-.8Z"/></>,
  menu: <><path d="M4 6h16M4 12h16M4 18h16"/></>,
  braces: <><path d="M8 3H6a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2 2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h2M16 3h2a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2 2 2 0 0 0-2 2v4a2 2 0 0 1-2 2h-2"/></>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></>,
  ban: <><circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/></>,
  ticket: <><path d="M4 7a2 2 0 0 0 0 4v6h16v-6a2 2 0 0 0 0-4V5H4Z"/><path d="M9 8h6M9 12h6"/></>,
}

function make(kind: string) {
  return function Icon({ size = 18, ...props }: Props) {
    return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[kind] || paths.menu}</svg>
  }
}

export const Activity=make('activity'), ArrowRight=make('arrow'), BadgeCheck=make('check'), Ban=make('ban'), BarChart3=make('chart'), Bot=make('bot'), Braces=make('braces'), Check=make('check'), CheckCircle2=make('check'), ChevronRight=make('chevron'), Clock3=make('clock'), Database=make('database'), Download=make('upload'), FileJson=make('braces'), FileText=make('file'), FlaskConical=make('activity'), GitBranch=make('link'), Info=make('info'), LayoutDashboard=make('menu'), Link2=make('link'), LoaderCircle=make('refresh'), LockKeyhole=make('lock'), MessageSquareText=make('file'), PauseCircle=make('pause'), Play=make('play'), Radar=make('activity'), RefreshCw=make('refresh'), ShieldAlert=make('alert'), ShieldCheck=make('shield'), Sparkles=make('sparkles'), TicketCheck=make('ticket'), Upload=make('upload'), Wifi=make('activity'), WifiOff=make('x'), X=make('x'), XCircle=make('x')
