module.exports = {
  darkMode: "class",
  content: [
    "./templates/**/*.html",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "sans-serif"],
      },
      colors: {
        keepup: {
          bg: "#07111f",
          panel: "#111c31",
          panelAlt: "#16253f",
          accent: "#38bdf8",
        },
      },
      boxShadow: {
        panel: "0 18px 40px rgba(7, 17, 31, 0.28)",
      },
    },
  },
  plugins: [],
};
