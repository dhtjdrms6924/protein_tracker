importScripts("https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.12.0/firebase-messaging-compat.js");

firebase.initializeApp({
  apiKey: "AIzaSyD-Gwg_r4x4CwRI3guw_kTE_41_obTqFb4",
  authDomain: "protein-tracker-f90d3.firebaseapp.com",
  projectId: "protein-tracker-f90d3",
  storageBucket: "protein-tracker-f90d3.firebasestorage.app",
  messagingSenderId: "431023920323",
  appId: "1:431023920323:web:d888dd8f6812f6c701be76"
});

const messaging = firebase.messaging();

// 백그라운드 메시지 처리
messaging.onBackgroundMessage((payload) => {
  const { title, body } = payload.notification || {};
  self.registration.showNotification(title || "단백질 트래커", {
    body: body || "",
    icon: "/static/icon-192.png",
    badge: "/static/icon-72.png",
    data: payload.data || {}
  });
});

// 알림 클릭 시 앱 열기
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && "focus" in client) {
          return client.focus();
        }
      }
      return clients.openWindow("/");
    })
  );
});