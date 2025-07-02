// Import the functions you need from the SDKs you need
import { initializeApp } from "firebase/app";
import { getAnalytics } from "firebase/analytics";
// TODO: Add SDKs for Firebase products that you want to use
// https://firebase.google.com/docs/web/setup#available-libraries

// Your web app's Firebase configuration
// For Firebase JS SDK v7.20.0 and later, measurementId is optional
const firebaseConfig = {
  apiKey: "AIzaSyCr5QOkwsIjXHr6DLE9J847dJTrxSX3t7s",
  authDomain: "kikichatbot-5d6a1.firebaseapp.com",
  projectId: "kikichatbot-5d6a1",
  storageBucket: "kikichatbot-5d6a1.firebasestorage.app",
  messagingSenderId: "993542388047",
  appId: "1:993542388047:web:23fe420e3539d92e6c6d3a",
  measurementId: "G-ZHD2D5T915"
};

// Initialize Firebase
const app = initializeApp(firebaseConfig);
const analytics = getAnalytics(app);
