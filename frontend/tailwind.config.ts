import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Calm neutral + single indigo/teal accent (dashboard/dev-tool tone).
        accent: { DEFAULT: "#4f46e5", soft: "#eef2ff" },
      },
    },
  },
  plugins: [],
} satisfies Config;
