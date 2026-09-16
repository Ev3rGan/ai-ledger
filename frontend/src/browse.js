import { createApp } from "vue";

import BrowseApp from "./BrowseApp.vue";
import "./public-interactions.css";

const mountPoint = document.querySelector('[data-vue-mount="browse"]');
const bootstrap = document.getElementById("browse-bootstrap");

if (mountPoint && bootstrap) {
  const initialState = JSON.parse(bootstrap.textContent);
  createApp(BrowseApp, { initialState }).mount(mountPoint);
}
