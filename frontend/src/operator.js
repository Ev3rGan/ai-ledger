import { createApp } from "vue";

import OperatorApp from "./OperatorApp.vue";
import "./operator.css";

const root = document.querySelector("#operator-app");
if (root) createApp(OperatorApp).mount(root);
