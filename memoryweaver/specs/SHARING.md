# Adaptive Share Event Design and Implementation Specs

This document specifies the design details for the unified, adaptive sharing interface.

## User Experience and Flow

1. **Trigger**: A single "Share" button replaces the clutter of separate "Copy Link" and "Copy QR" options in each event row.
2. **Modal View**: Displays a clean modal overlay containing:
   - Dynamic QR Code (rendered to canvas).
   - Invitation Link with one-click copy.
   - Pre-formatted Invite Text.
   - **Quick Actions Row**: Launches Email (`mailto:`), WhatsApp (`api.whatsapp`), or SMS (`sms:`) directly.
   - **Desktop Utilities Grid**: Download QR PNG, Copy QR Image to clipboard.
   - **Native Share Button**: Triggers `navigator.share` (automatically shown on supporting mobile browsers).

## Browser / OS Compatibility Matrix

| Feature | Desktop Chrome/Safari | Mobile Safari/Chrome | Fallback |
| :--- | :--- | :--- | :--- |
| **Copy Link** | Yes | Yes | Manual copy |
| **Copy QR Image** | Yes | Yes | Download QR file |
| **Download QR** | Yes | Yes | Show image context menu |
| **Email Invite** | Yes (`mailto:`) | Yes (`mailto:`) | None |
| **WhatsApp Invite** | Yes (`web.whatsapp.com`) | Yes (WhatsApp App) | None |
| **SMS Invite** | No | Yes (`sms:`) | Hide button |
| **Native Share** | Yes (macOS/Windows Chrome) | Yes (iOS/Android Safari/Chrome) | Hide button |

## Implementation Files

- **HTML/CSS/JS**: [upload.html](file:///Users/mitilroy/Dev/src/MysticRuse/MemoryWeaver/memoryweaver/frontend/upload.html)
