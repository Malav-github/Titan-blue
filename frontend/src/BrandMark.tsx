// The app's icon: a pen nib (the apostrophe) on a block. The same drawing as public/favicon.svg.
export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg viewBox="0 0 120 120" width={size} height={size} aria-hidden="true" className="shrink-0">
      <defs><clipPath id="brand-tile"><rect width="120" height="120" rx="26" /></clipPath></defs>
      <rect width="120" height="120" rx="26" fill="#141821" />
      <g clipPath="url(#brand-tile)">
        <g transform="rotate(14 60 50)">
          <path d="M60 106 L34 46 C34 24 44 6 60 -16 C76 6 86 24 86 46 Z" fill="#2F5BEA" />
          <path d="M60 104 L60 54" stroke="#141821" strokeWidth="4" strokeLinecap="round" />
          <circle cx="60" cy="46" r="7.5" fill="#141821" />
        </g>
      </g>
    </svg>
  )
}
