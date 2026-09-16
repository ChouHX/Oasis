import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider, App as AntApp } from "antd";
import zhCN from "antd/locale/zh_CN";
import "antd/dist/reset.css";
import App from "./App.jsx";

// antd's own design language, as asked for - no token overrides. Chinese locale
// because the whole console is Chinese.
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN}>
      <AntApp>
        <App />
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>
);
