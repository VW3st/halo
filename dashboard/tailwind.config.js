/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        cyan: { DEFAULT: '#00e5ff', dim: '#0099b3' },
        violet: { DEFAULT: '#b794f4', dim: '#7c5ed6' },
        amber: { DEFAULT: '#ffaa3b' },
        emerald: { DEFAULT: '#34d399' },
        rose: { DEFAULT: '#ff5d6c' },
        ink: { 0: '#06080d', 1: '#0a0e17', 2: '#10182a' },
        edge: 'rgba(0,229,255,0.16)',
        dim: '#7a8ba6',
        faint: '#3a4861',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'ui-monospace', 'SFMono-Regular', 'Consolas', 'monospace'],
        ui: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'system-ui', 'sans-serif'],
      },
      keyframes: {
        blip: {
          '0%,100%': { opacity: '0.4', transform: 'scale(0.85)' },
          '50%': { opacity: '1', transform: 'scale(1.1)' },
        },
      },
      animation: { blip: 'blip 2s ease-in-out infinite' },
    },
  },
  plugins: [],
}
