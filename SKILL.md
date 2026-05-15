# Supanova Design Skill - Combined

Source: https://github.com/uxjoseph/supanova-design-skill

## Output Rules (output-skill)
- Treat every landing page generation as production-critical. A partial output is a broken output.
- No HTML comments like `<!-- ... -->` or `<!-- add more sections as needed -->`
- Never say "Let me know if you want me to continue"
- Full page required: navigation, hero, social proof, features, testimonials, CTA, footer
- Real Korean content (no placeholder text)
- Complete responsive classes across breakpoints
- Hover/active states for interactive elements

## Technical Stack (taste-skill)
- Single HTML file output with inline styles and scripts
- Pretendard font (mandatory for Korean text)
- Tailwind CSS via CDN with custom theme extensions
- Iconify Solar icons exclusively — no emojis
- Motion One library for animations when intensity > 5
- min-h-[100dvh] not h-screen (iOS Safari compatibility)

## Design Philosophy (soft-skill)
- Three vibe archetypes: Vantablack Luxe, Warm Editorial, Clean Structural
- Double-Bezel card pattern — outer glass shell with inner core
- cubic-bezier(0.16, 1, 0.3, 1) easing curve for all animations
- Only transform and opacity for GPU-efficient animations
- IntersectionObserver for scroll-triggered entrance sequences

## Typography
- Korean headlines: leading-snug + break-keep-all
- Body text: max-w-[65ch]
- Banned fonts: Inter, Noto Sans KR, Roboto

## Color Rules
- Maximum one accent color per page, saturation below 80%
- No purple-blue "AI" gradients
- Dark mode defaults for premium appearance
- No pure black (#000000)

## Layout
- No centered hero sections when design variance > 4
- Split-screen, asymmetric, or full-bleed patterns
- Each section visually distinct from adjacent sections
- No 3-column equal card layouts

## Korean Content
- No "혁신적인", "차세대" clichés
- CTAs: concrete action language like "무료로 시작하기"
- Specific metrics: "47,200+" not "50,000+"
- Realistic Korean names and company titles
