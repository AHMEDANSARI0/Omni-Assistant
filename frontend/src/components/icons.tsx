/**
 * A hand-built icon set (20 stroke icons, one visual language).
 * No icon package: the dashboard ships ~40 glyphs, and a dependency would be
 * larger than the whole set.
 */
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

const base = (size: number): SVGProps<SVGSVGElement> => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
  focusable: false,
});

export const IconCenter = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M4 9V6a2 2 0 0 1 2-2h3M20 9V6a2 2 0 0 0-2-2h-3M4 15v3a2 2 0 0 0 2 2h3M20 15v3a2 2 0 0 1-2 2h-3" /><circle cx="12" cy="12" r="2.4" /></svg>
);
export const IconCore = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><circle cx="12" cy="12" r="3" /><ellipse cx="12" cy="12" rx="9" ry="4.2" /><ellipse cx="12" cy="12" rx="4.2" ry="9" /></svg>
);
export const IconAgents = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><circle cx="9" cy="8" r="3" /><path d="M3.5 19a5.5 5.5 0 0 1 11 0" /><path d="M16 6.5a2.5 2.5 0 1 1 3.2 3.2A4.5 4.5 0 0 1 21 14h-2.4" /></svg>
);
export const IconMission = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M12 3l2.6 5.6 6.1.8-4.5 4.2 1.2 6-5.4-3-5.4 3 1.2-6L3.3 9.4l6.1-.8z" /></svg>
);
export const IconTasks = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M4 6h10M4 12h10M4 18h7" /><path d="M18 5l2 2 2-2" transform="translate(-2 2)" /></svg>
);
export const IconCalendar = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><rect x="3.5" y="5" width="17" height="15" rx="2.5" /><path d="M3.5 10h17M8 3.5V6M16 3.5V6" /></svg>
);
export const IconMemory = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M7 4h10a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z" /><path d="M9 9h6M9 13h6M9 17h3" /></svg>
);
export const IconChat = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7A2.5 2.5 0 0 1 17.5 16H10l-4.5 4v-4H6.5A2.5 2.5 0 0 1 4 13.5z" /></svg>
);
export const IconKnowledge = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H19v15H6.5A2.5 2.5 0 0 0 4 20.5z" /><path d="M8 7.5h7M8 11h5" /></svg>
);
export const IconTools = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M14.5 6.5a3.5 3.5 0 1 0 4 4L21 8l-2-2z" transform="translate(-1 0)" /><path d="M13 9L4.5 17.5a2.1 2.1 0 0 0 3 3L16 12" /></svg>
);
export const IconFlow = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><rect x="3" y="4" width="6" height="5" rx="1.5" /><rect x="15" y="15" width="6" height="5" rx="1.5" /><path d="M6 9v4a3 3 0 0 0 3 3h6" /></svg>
);
export const IconModel = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><rect x="6" y="6" width="12" height="12" rx="3" /><path d="M12 2.5V6M12 18v3.5M2.5 12H6M18 12h3.5M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2" /></svg>
);
export const IconVoice = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M9 21h6" /></svg>
);
export const IconShield = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M12 3l7 3v5.5c0 4.3-2.9 7.8-7 9.5-4.1-1.7-7-5.2-7-9.5V6z" /><path d="M9.2 12.2l2 2 3.6-3.8" /></svg>
);
export const IconMonitor = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /><path d="M6.5 12.5l3-3 2.5 2.5 4-4.5" /></svg>
);
export const IconAnalytics = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M4 19V5M4 19h16" /><rect x="7" y="11" width="3" height="5" rx="1" /><rect x="12" y="8" width="3" height="8" rx="1" /><rect x="17" y="13" width="3" height="3" rx="1" /></svg>
);
export const IconSettings = ({ size = 18, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><circle cx="12" cy="12" r="3" /><path d="M12 3v2.2M12 18.8V21M4.6 7.6l1.9 1.1M17.5 15.3l1.9 1.1M4.6 16.4l1.9-1.1M17.5 8.7l1.9-1.1" /></svg>
);
export const IconSearch = ({ size = 16, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></svg>
);
export const IconBell = ({ size = 16, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M6.5 9.5a5.5 5.5 0 0 1 11 0c0 3.2.8 4.6 1.6 5.6H4.9c.8-1 1.6-2.4 1.6-5.6z" /><path d="M10 18.5a2 2 0 0 0 4 0" /></svg>
);
export const IconSun = ({ size = 16, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><circle cx="12" cy="12" r="4" /><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19" /></svg>
);
export const IconMoon = ({ size = 16, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a6.8 6.8 0 0 0 10.5 10.5z" /></svg>
);
export const IconStop = ({ size = 16, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><rect x="6" y="6" width="12" height="12" rx="2.5" /></svg>
);
export const IconSend = ({ size = 16, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M4 12l16-7-7 16-2.5-6.5z" /></svg>
);
export const IconMic = ({ size = 16, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><rect x="9.2" y="3.2" width="5.6" height="10.6" rx="2.8" /><path d="M5.6 11.2a6.4 6.4 0 0 0 12.8 0M12 17.6V21" /></svg>
);
export const IconChevron = ({ size = 14, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M9 5l7 7-7 7" /></svg>
);
export const IconRefresh = ({ size = 14, ...rest }: IconProps) => (
  <svg {...base(size)} {...rest}><path d="M20 12a8 8 0 1 1-2.6-5.9" /><path d="M20 4.5V10h-5.4" /></svg>
);
export const IconLogo = ({ size = 30, ...rest }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 40 40" fill="none" aria-hidden focusable="false" {...rest}>
    <circle cx="20" cy="20" r="17.2" stroke="rgb(var(--accent))" strokeOpacity="0.55" strokeWidth="1.1" />
    <circle cx="20" cy="20" r="12.6" stroke="rgb(var(--accent))" strokeOpacity="0.28" strokeWidth="1" strokeDasharray="3 5" />
    <path d="M20 7.6l3.7 8.7 9.1 1.2-6.8 6.2 1.9 9-7.9-4.5-7.9 4.5 1.9-9-6.8-6.2 9.1-1.2z" fill="url(#omniGrad)" fillOpacity="0.9" />
    <circle cx="20" cy="20" r="3.1" fill="rgb(var(--surface))" stroke="rgb(var(--accent))" strokeWidth="1.2" />
    <defs>
      <linearGradient id="omniGrad" x1="8" y1="8" x2="32" y2="32" gradientUnits="userSpaceOnUse">
        <stop stopColor="rgb(var(--accent))" />
        <stop offset="1" stopColor="rgb(var(--violet))" />
      </linearGradient>
    </defs>
  </svg>
);
