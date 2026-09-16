import { createApp } from "vue";

import ResearchApp from "./ResearchApp.vue";
import "./public-interactions.css";
import { ResearchStreamClient } from "./research-stream-client.js";

const mountPoint = document.querySelector('[data-vue-mount="research"]');
const bootstrap = document.getElementById("research-bootstrap");

if (mountPoint && bootstrap) {
  const { examples } = JSON.parse(bootstrap.textContent);
  createApp(ResearchApp, {
    client: new ResearchStreamClient(),
    examples,
  }).mount(mountPoint);
}
