import os
import sys
import csv
import time
import threading
from datetime import datetime
from typing import Dict, Optional, List, Callable
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk
import serial
import serial.tools.list_ports

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BOOKINGS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookings.csv")
CSV_HEADER = ["Logged At", "Event", "Ticket ID", "Slot", "Vehicle Number", "Driver Name", "Phone", "Booked At"]


def log_booking(event: str, slot_num: int, info: Dict, path: str = BOOKINGS_CSV):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CSV_HEADER)
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), event, info["ticket"], slot_num,
                    info["plate"], info["name"], info["phone"], info["time"]])


def bring_to_front(win, focus_widget=None):
    # CTkToplevel resets its stacking ~200ms after creation (shows up behind the main window on Windows),
    # so raise it after that and make it modal so a second click can't open a duplicate dialog.
    def _raise():
        win.lift()
        win.focus_force()
        (focus_widget or win).focus_set()
        try:
            win.grab_set()
        except tk.TclError:
            pass
    win.after(250, _raise)


class SerialService:
    def __init__(self, on_status_received: Callable[[Dict], None] = None,
                 on_rfid_event: Callable[[str, str], None] = None,
                 on_connection_changed: Callable[[bool, str], None] = None,
                 on_book_failed: Callable[[int], None] = None):
        self.on_status_received = on_status_received
        self.on_rfid_event = on_rfid_event
        self.on_connection_changed = on_connection_changed
        self.on_book_failed = on_book_failed

        self.serial_port: Optional[serial.Serial] = None
        self.is_connected = False
        self.running = False
        self.read_thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self.slots_ir = [0, 0, 0, 0]
        self.slots_booked = [False, False, False, False]
        self.gate_open = False

    @staticmethod
    def get_available_ports() -> List[str]:
        try:
            ports = serial.tools.list_ports.comports()
            usb_ports = []
            other_ports = []

            for p in ports:
                name = p.device
                lower = name.lower()
                desc = (p.description or "").lower()

                if any(x in lower or x in desc for x in ["bluetooth", "soundcore", "airdopes", "airpods", "headphone", "audio", "printer"]):
                    continue

                if any(x in lower or x in desc for x in ["usb", "modem", "serial", "ch340", "cp210", "ftdi", "arduino", "com"]):
                    usb_ports.append(name)
                else:
                    other_ports.append(name)

            filtered = usb_ports + other_ports
            return filtered if filtered else ["No Device Detected"]
        except Exception:
            return ["No Device Detected"]

    def connect_async(self, port: str, baudrate: int = 9600):
        thread = threading.Thread(target=self._connect_worker, args=(port, baudrate), daemon=True)
        thread.start()

    def _connect_worker(self, port: str, baudrate: int):
        try:
            ser = serial.Serial(port=port, baudrate=baudrate, timeout=0.5)
            # opening the port resets the Arduino: bootloader + 1.2s LCD splash in setup(); commands sent earlier are lost
            time.sleep(2.5)

            with self._lock:
                self.serial_port = ser
                self.is_connected = True
                self.running = True

            self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.read_thread.start()

            if self.on_connection_changed:
                self.on_connection_changed(True, f"Connected: {port}")

            self.send_command("CMD:GET_STATUS")

        except Exception as e:
            with self._lock:
                self.is_connected = False
                self.running = False
                self.serial_port = None
            if self.on_connection_changed:
                self.on_connection_changed(False, f"Connection Failed: {str(e)}")

    def disconnect(self):
        with self._lock:
            self.running = False
            self.is_connected = False
            if self.serial_port and self.serial_port.is_open:
                try:
                    self.serial_port.close()
                except Exception:
                    pass
            self.serial_port = None

        if self.on_connection_changed:
            self.on_connection_changed(False, "Disconnected")

    def send_command(self, cmd: str):
        if not self.is_connected:
            return

        with self._lock:
            sp = self.serial_port

        if sp and sp.is_open:
            try:
                sp.write((cmd + "\n").encode('utf-8'))
                sp.flush()
            except Exception:
                pass

    def _read_loop(self):
        buffer = ""
        while self.running:
            with self._lock:
                sp = self.serial_port
                if not sp or not sp.is_open:
                    break

            try:
                if sp.in_waiting > 0:
                    data = sp.read(min(sp.in_waiting, 512)).decode('utf-8', errors='ignore')
                    buffer += data

                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip("\r ").strip()
                        if line:
                            self._parse_line(line)
                else:
                    time.sleep(0.04)
            except Exception:
                break

        if self.running:
            self.disconnect()

    def _parse_line(self, line: str):
        if line.startswith("STATUS:"):
            payload = line[7:]
            parts = payload.split(",")
            status_data = {}
            for part in parts:
                if "=" in part:
                    k, v = part.split("=", 1)
                    k = k.strip()
                    try:
                        status_data[k] = int(v.strip())
                    except ValueError:
                        status_data[k] = v.strip()

            with self._lock:
                for i in range(4):
                    s_key = f"S{i+1}"
                    b_key = f"B{i+1}"
                    if s_key in status_data:
                        self.slots_ir[i] = status_data[s_key]
                    if b_key in status_data:
                        self.slots_booked[i] = (status_data[b_key] == 1)
                if "GATE" in status_data:
                    self.gate_open = (status_data["GATE"] == 1)

            if self.on_status_received:
                self.on_status_received(status_data)

        elif line.startswith("RFID:AUTH:"):
            card_id = line[10:].strip()
            with self._lock:
                self.gate_open = True
            if self.on_rfid_event:
                self.on_rfid_event(card_id, "AUTHORIZED")

        elif line.startswith("RFID:INVALID:"):
            card_id = line[13:].strip()
            if self.on_rfid_event:
                self.on_rfid_event(card_id, "INVALID")

        elif line.startswith("ACK:BOOK_FAIL:"):
            if self.on_book_failed:
                try:
                    self.on_book_failed(int(line[14:]))
                except ValueError:
                    pass

        elif line == "GATE:OPEN":
            with self._lock:
                self.gate_open = True
            if self.on_status_received:
                self.on_status_received({"GATE": 1})

        elif line == "GATE:CLOSED":
            with self._lock:
                self.gate_open = False
            if self.on_status_received:
                self.on_status_received({"GATE": 0})


class BookingDialog(ctk.CTkToplevel):
    def __init__(self, parent, slot_num: int, on_confirmed):
        super().__init__(parent)
        self.slot_num = slot_num
        self.on_confirmed = on_confirmed

        self.title(f"Book Slot {slot_num}")
        self.geometry("450x480")
        self.resizable(False, False)
        self.configure(fg_color="#0F172A")

        self.transient(parent)
        self.lift()
        self.focus_force()

        self.update_idletasks()
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            x = px + (pw // 2) - 225
            y = py + (ph // 2) - 240
            self.geometry(f"+{max(50, x)}+{max(50, y)}")
        except Exception:
            pass

        self._build_ui()

    def _build_ui(self):
        container = ctk.CTkFrame(self, fg_color="#1E293B", corner_radius=16, border_width=1, border_color="#334155")
        container.pack(fill="both", expand=True, padx=16, pady=16)

        badge = ctk.CTkFrame(container, fg_color="#064E3B", corner_radius=14, border_width=1, border_color="#059669")
        badge.pack(pady=(16, 8))
        ctk.CTkLabel(badge, text=f"SLOT {self.slot_num:02d}", font=ctk.CTkFont(size=13, weight="bold"), text_color="#10B981").pack(padx=14, pady=3)

        ctk.CTkLabel(container, text="Reserve Parking Slot", font=ctk.CTkFont(size=20, weight="bold"), text_color="#F8FAFC").pack(pady=(0, 2))
        ctk.CTkLabel(container, text="Fill in the details below to complete your booking", font=ctk.CTkFont(size=12), text_color="#94A3B8").pack(pady=(0, 16))

        form = ctk.CTkFrame(container, fg_color="transparent")
        form.pack(fill="x", padx=20)

        ctk.CTkLabel(form, text="Vehicle Registration Number *", font=ctk.CTkFont(size=12, weight="bold"), text_color="#E2E8F0").pack(anchor="w", pady=(0, 2))
        self.plate_entry = ctk.CTkEntry(form, placeholder_text="e.g. MH-12-AB-1234", height=38, font=ctk.CTkFont(size=13), fg_color="#0F172A", border_color="#475569")
        self.plate_entry.pack(fill="x", pady=(0, 12))
        bring_to_front(self, self.plate_entry)

        ctk.CTkLabel(form, text="Driver Full Name *", font=ctk.CTkFont(size=12, weight="bold"), text_color="#E2E8F0").pack(anchor="w", pady=(0, 2))
        self.name_entry = ctk.CTkEntry(form, placeholder_text="e.g. Alex Morgan", height=38, font=ctk.CTkFont(size=13), fg_color="#0F172A", border_color="#475569")
        self.name_entry.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(form, text="Contact Phone Number (Optional)", font=ctk.CTkFont(size=12, weight="bold"), text_color="#E2E8F0").pack(anchor="w", pady=(0, 2))
        self.phone_entry = ctk.CTkEntry(form, placeholder_text="e.g. +91 98765 43210", height=38, font=ctk.CTkFont(size=13), fg_color="#0F172A", border_color="#475569")
        self.phone_entry.pack(fill="x", pady=(0, 18))

        btn_box = ctk.CTkFrame(container, fg_color="transparent")
        btn_box.pack(fill="x", padx=20, pady=(0, 12))

        cancel_btn = ctk.CTkButton(btn_box, text="Cancel", fg_color="#334155", hover_color="#475569", height=40, corner_radius=8, font=ctk.CTkFont(size=13, weight="bold"), command=self.destroy)
        cancel_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        confirm_btn = ctk.CTkButton(btn_box, text="Confirm & Book Slot", fg_color="#10B981", hover_color="#059669", height=40, corner_radius=8, font=ctk.CTkFont(size=13, weight="bold"), command=self._confirm)
        confirm_btn.pack(side="right", fill="x", expand=True, padx=(6, 0))

    def _confirm(self):
        plate = self.plate_entry.get().strip().upper()
        name = self.name_entry.get().strip()
        phone = self.phone_entry.get().strip()

        if not plate:
            messagebox.showwarning("Required Field", "Please enter your vehicle registration plate number.", parent=self)
            self.plate_entry.focus_set()
            return

        if not name:
            messagebox.showwarning("Required Field", "Please enter driver's name.", parent=self)
            self.name_entry.focus_set()
            return

        self.on_confirmed(self.slot_num, plate, name, phone)
        self.destroy()


class BookingSuccessModal(ctk.CTkToplevel):
    def __init__(self, parent, slot_num: int, info: Dict):
        super().__init__(parent)
        self.title("Booking Confirmed!")
        self.geometry("460x510")
        self.resizable(False, False)
        self.configure(fg_color="#0F172A")

        self.transient(parent)
        bring_to_front(self)

        self.slot_num = slot_num
        self.plate = info["plate"]
        self.name = info["name"]
        self.ticket_id = info["ticket"]
        self.booking_time = info["time"]

        self.update_idletasks()
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            x = px + (pw // 2) - 230
            y = py + (ph // 2) - 255
            self.geometry(f"+{max(50, x)}+{max(50, y)}")
        except Exception:
            pass

        self._build_ui()

    def _build_ui(self):
        container = ctk.CTkFrame(self, fg_color="#1E293B", corner_radius=18, border_width=1, border_color="#334155")
        container.pack(fill="both", expand=True, padx=16, pady=16)

        icon_box = ctk.CTkFrame(container, width=60, height=60, fg_color="#064E3B", corner_radius=30, border_width=2, border_color="#10B981")
        icon_box.pack(pady=(18, 10))
        icon_box.pack_propagate(False)
        ctk.CTkLabel(icon_box, text="✓", font=ctk.CTkFont(size=30, weight="bold"), text_color="#10B981").place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(container, text="Thank You For Booking!", font=ctk.CTkFont(size=22, weight="bold"), text_color="#F8FAFC").pack(pady=(0, 2))
        ctk.CTkLabel(container, text=f"Slot {self.slot_num} has been successfully reserved for you.", font=ctk.CTkFont(size=12), text_color="#94A3B8").pack(pady=(0, 14))

        ticket = ctk.CTkFrame(container, fg_color="#0F172A", corner_radius=12, border_width=1, border_color="#334155")
        ticket.pack(fill="x", padx=20, pady=(0, 16))

        t_strip = ctk.CTkFrame(ticket, fg_color="#1E293B", corner_radius=8, height=32)
        t_strip.pack(fill="x", padx=10, pady=(10, 8))
        t_strip.pack_propagate(False)
        ctk.CTkLabel(t_strip, text=f"PASS CODE: {self.ticket_id}", font=ctk.CTkFont(size=11, weight="bold"), text_color="#38BDF8").pack(side="left", padx=10)
        ctk.CTkLabel(t_strip, text="RESERVED", font=ctk.CTkFont(size=10, weight="bold"), text_color="#10B981").pack(side="right", padx=10)

        items = [
            ("Reserved Slot", f"Slot {self.slot_num}"),
            ("Vehicle Number", self.plate),
            ("Driver Name", self.name),
            ("Booking Time", self.booking_time),
            ("Gate Access", "Show Registered RFID Card")
        ]

        for lbl, val in items:
            row = ctk.CTkFrame(ticket, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=3)
            ctk.CTkLabel(row, text=lbl, font=ctk.CTkFont(size=11), text_color="#64748B").pack(side="left")
            ctk.CTkLabel(row, text=val, font=ctk.CTkFont(size=11, weight="bold"), text_color="#E2E8F0").pack(side="right")

        ctk.CTkFrame(ticket, height=8, fg_color="transparent").pack()

        note = ctk.CTkLabel(container, text="When you arrive, scan your RFID card at the gate to enter.\nYour reserved slot will turn RED once parked.",
                            font=ctk.CTkFont(size=11), text_color="#94A3B8", justify="center")
        note.pack(pady=(0, 14))

        done_btn = ctk.CTkButton(container, text="Done & Return to Dashboard", fg_color="#3B82F6", hover_color="#2563EB", height=38, corner_radius=8, font=ctk.CTkFont(size=13, weight="bold"), command=self.destroy)
        done_btn.pack(fill="x", padx=20, pady=(0, 12))


class ParkingSlotCard(ctk.CTkFrame):
    # state: (pill text, accent, pill tint, button text, button color, button hover)
    STYLES = {
        "FREE": ("AVAILABLE", "#10B981", "#052E22", "Book Slot", "#10B981", "#059669"),
        "BOOKED": ("RESERVED", "#F59E0B", "#2B1A05", "Cancel Booking", "#D97706", "#B45309"),
        "OCCUPIED": ("OCCUPIED", "#EF4444", "#2A0B0B", "Slot Full", "#374151", "#374151"),
    }

    def __init__(self, parent, slot_num: int, on_book_click, on_release_click):
        super().__init__(parent, fg_color="#111827", corner_radius=14, border_width=1, border_color="#1F2937")
        self.slot_num = slot_num
        self.on_book_click = on_book_click
        self.on_release_click = on_release_click

        self.current_state = None
        self.booking_info = None

        self._build_ui()
        self.update_state(is_occupied=False, is_booked=False)

    def _build_ui(self):
        self.accent_bar = ctk.CTkFrame(self, height=4, corner_radius=2)
        self.accent_bar.pack(fill="x", padx=16, pady=(12, 0))

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(10, 6))
        ctk.CTkLabel(head, text=f"SLOT {self.slot_num:02d}", font=ctk.CTkFont(size=16, weight="bold"), text_color="#F8FAFC").pack(side="left")
        self.status_lbl = ctk.CTkLabel(head, text="", height=22, corner_radius=11, font=ctk.CTkFont(size=10, weight="bold"))
        self.status_lbl.pack(side="right")

        self.canvas = tk.Canvas(self, height=100, bg="#0B1220", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=16, pady=4)
        self.canvas.bind("<Configure>", lambda e: self._draw_canvas())

        details = ctk.CTkFrame(self, fg_color="#0B1220", corner_radius=8)
        details.pack(fill="x", padx=16, pady=(6, 8))
        self.vehicle_lbl = self._detail_row(details, "Vehicle", pady=(6, 0))
        self.driver_lbl = self._detail_row(details, "Driver", pady=(0, 6))

        self.action_btn = ctk.CTkButton(self, height=34, corner_radius=8, font=ctk.CTkFont(size=12, weight="bold"), command=self._handle_button_action)
        self.action_btn.pack(fill="x", padx=16, pady=(0, 14))

    @staticmethod
    def _detail_row(parent, label: str, pady):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=pady)
        ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=11), text_color="#64748B", height=20).pack(side="left")
        value = ctk.CTkLabel(row, text="—", font=ctk.CTkFont(size=11, weight="bold"), text_color="#E2E8F0", height=20)
        value.pack(side="right")
        return value

    def _handle_button_action(self):
        if self.current_state == "FREE":
            self.on_book_click(self.slot_num)
        elif self.current_state == "BOOKED":
            self.on_release_click(self.slot_num)

    def update_state(self, is_occupied: bool, is_booked: bool, booking_info: Optional[Dict] = None):
        self.booking_info = booking_info
        info = booking_info or {}
        self.vehicle_lbl.configure(text=info.get("plate", "—"))
        self.driver_lbl.configure(text=info.get("name", "—"))

        state = "OCCUPIED" if is_occupied else ("BOOKED" if is_booked else "FREE")
        if state == self.current_state:
            # status arrives every ~1.2s; reconfiguring the button each time can swallow a click
            return
        self.current_state = state

        pill, accent, tint, btn_text, btn_color, btn_hover = self.STYLES[state]
        self.accent_bar.configure(fg_color=accent)
        self.status_lbl.configure(text=f"   {pill}   ", fg_color=tint, text_color=accent)
        self.action_btn.configure(text=btn_text, fg_color=btn_color, hover_color=btn_hover,
                                  text_color="#9CA3AF" if state == "OCCUPIED" else "#FFFFFF",
                                  state="disabled" if state == "OCCUPIED" else "normal")
        self._draw_canvas()

    def _draw_canvas(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 40 or h < 40 or self.current_state is None:
            return
        accent = self.STYLES[self.current_state][1]

        # bay markings: open at the bottom (entry side)
        m = 12
        for x1, y1, x2, y2 in [(m, m, m, h - m), (w - m, m, w - m, h - m), (m, m, w - m, m)]:
            c.create_line(x1, y1, x2, y2, fill="#334155", width=3)

        cx, cy = w // 2, h // 2 + 3
        if self.current_state == "OCCUPIED":
            cw, ch = 46, min(78, h - 2 * m - 8)
            x1, y1, x2, y2 = cx - cw // 2, cy - ch // 2, cx + cw // 2, cy + ch // 2
            for wx in (x1 - 4, x2):
                for wy in (y1 + 8, y2 - 20):
                    c.create_rectangle(wx, wy, wx + 4, wy + 12, fill="#0F172A", outline="")
            c.create_rectangle(x1, y1, x2, y2, fill="#DC2626", outline="#F87171", width=2)
            c.create_rectangle(x1 + 6, y1 + 12, x2 - 6, y1 + 24, fill="#1E293B", outline="")
            c.create_rectangle(x1 + 6, y2 - 16, x2 - 6, y2 - 8, fill="#1E293B", outline="")
            c.create_rectangle(x1 + 3, y1 + 2, x1 + 11, y1 + 5, fill="#FEF08A", outline="")
            c.create_rectangle(x2 - 11, y1 + 2, x2 - 3, y1 + 5, fill="#FEF08A", outline="")
        elif self.current_state == "BOOKED":
            c.create_rectangle(m + 10, m + 10, w - m - 10, h - m - 4, outline=accent, width=1, dash=(4, 3))
            c.create_text(cx, cy - 8, text="RESERVED", font=("Helvetica", 13, "bold"), fill=accent)
            plate = (self.booking_info or {}).get("plate", "")
            if plate:
                c.create_text(cx, cy + 14, text=plate, font=("Courier", 11, "bold"), fill="#E2E8F0")
        else:
            r = 20
            c.create_oval(cx - r, cy - r - 4, cx + r, cy + r - 4, outline=accent, width=2)
            c.create_text(cx, cy - 4, text="P", font=("Helvetica", 18, "bold"), fill=accent)


class ParkingApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Smart Car Parking")
        self.geometry("1160x730")
        self.minsize(1020, 650)
        self.configure(fg_color="#0B0F19")

        self.serial_service = SerialService(
            on_status_received=self._on_status_received,
            on_rfid_event=self._on_rfid_event,
            on_connection_changed=self._on_conn_changed,
            on_book_failed=self._on_book_failed
        )

        self.bookings: Dict[int, Dict] = {}
        self.rfid_history: List[str] = []
        self.slot_cards: List[ParkingSlotCard] = []

        self._build_header()
        self._build_stats_ribbon()
        self._build_main_content()
        self._build_footer()

        self._refresh_ports()

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="#111827", corner_radius=0, height=64)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.pack(side="left", padx=20)

        badge = ctk.CTkFrame(brand, width=38, height=38, fg_color="#3B82F6", corner_radius=8)
        badge.pack(side="left", padx=(0, 10))
        badge.pack_propagate(False)
        ctk.CTkLabel(badge, text="🚗", font=ctk.CTkFont(size=18)).place(relx=0.5, rely=0.5, anchor="center")

        tbox = ctk.CTkFrame(brand, fg_color="transparent")
        tbox.pack(side="left")
        ctk.CTkLabel(tbox, text="Smart Car Parking", font=ctk.CTkFont(size=18, weight="bold"), text_color="#F8FAFC").pack(anchor="w")
        ctk.CTkLabel(tbox, text="Real-Time Slot Occupancy & Booking System", font=ctk.CTkFont(size=11), text_color="#94A3B8").pack(anchor="w")

        ctrl = ctk.CTkFrame(header, fg_color="transparent")
        ctrl.pack(side="right", padx=20)

        ctk.CTkLabel(ctrl, text="Port:", font=ctk.CTkFont(size=11), text_color="#94A3B8").pack(side="left", padx=(0, 4))
        self.port_combo = ctk.CTkComboBox(ctrl, values=["Scanning..."], width=160, height=32, font=ctk.CTkFont(size=11), fg_color="#1E293B", border_color="#334155")
        self.port_combo.pack(side="left", padx=(0, 4))

        ref_btn = ctk.CTkButton(ctrl, text="↻", width=32, height=32, font=ctk.CTkFont(size=14), fg_color="#1E293B", hover_color="#334155", command=self._refresh_ports)
        ref_btn.pack(side="left", padx=(0, 8))

        self.baud_combo = ctk.CTkOptionMenu(ctrl, values=["9600", "115200"], width=85, height=32, font=ctk.CTkFont(size=11), fg_color="#1E293B")
        self.baud_combo.set("9600")
        self.baud_combo.pack(side="left", padx=(0, 10))

        self.connect_btn = ctk.CTkButton(
            ctrl,
            text="Connect",
            width=100,
            height=32,
            corner_radius=6,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#10B981",
            hover_color="#059669",
            command=self._toggle_connection
        )
        self.connect_btn.pack(side="left")

    def _build_stats_ribbon(self):
        ribbon = ctk.CTkFrame(self, fg_color="transparent")
        ribbon.pack(fill="x", padx=20, pady=(12, 8))

        self.metric_cards = {}
        metrics = [
            ("TOTAL SLOTS", "4", "#3B82F6", "🅿️"),
            ("AVAILABLE SLOTS", "4", "#10B981", "🟢"),
            ("OCCUPIED (FULL)", "0", "#EF4444", "🔴"),
            ("BOOKED / RESERVED", "0", "#F59E0B", "🟡")
        ]

        for i, (label, val, color, emoji) in enumerate(metrics):
            card = ctk.CTkFrame(ribbon, fg_color="#111827", corner_radius=10, border_width=1, border_color="#1F2937")
            card.pack(side="left", fill="both", expand=True, padx=(0 if i == 0 else 6, 0 if i == 3 else 6))

            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=12, pady=8)

            left = ctk.CTkFrame(inner, fg_color="transparent")
            left.pack(side="left")
            ctk.CTkLabel(left, text=label, font=ctk.CTkFont(size=10, weight="bold"), text_color="#94A3B8").pack(anchor="w")
            lbl_val = ctk.CTkLabel(left, text=val, font=ctk.CTkFont(size=20, weight="bold"), text_color=color)
            lbl_val.pack(anchor="w")

            right_emoji = ctk.CTkLabel(inner, text=emoji, font=ctk.CTkFont(size=20))
            right_emoji.pack(side="right")

            self.metric_cards[label] = lbl_val

    def _build_main_content(self):
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=4)

        grid_container = ctk.CTkFrame(content, fg_color="transparent")
        grid_container.pack(side="left", fill="both", expand=True, padx=(0, 14))

        bays_frame = ctk.CTkFrame(grid_container, fg_color="transparent")
        bays_frame.pack(fill="both", expand=True)

        bays_frame.columnconfigure(0, weight=1)
        bays_frame.columnconfigure(1, weight=1)
        bays_frame.rowconfigure(0, weight=1)
        bays_frame.rowconfigure(1, weight=1)

        for i in range(4):
            slot_num = i + 1
            card = ParkingSlotCard(
                bays_frame,
                slot_num=slot_num,
                on_book_click=self._on_book_slot,
                on_release_click=self._on_release_slot
            )
            row = i // 2
            col = i % 2
            card.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
            self.slot_cards.append(card)

        side_panel = ctk.CTkFrame(content, width=340, fg_color="#111827", corner_radius=14, border_width=1, border_color="#1F2937")
        side_panel.pack(side="right", fill="both", expand=False)
        side_panel.pack_propagate(False)

        self._build_side_panel(side_panel)

    def _build_side_panel(self, parent):
        gate_box = ctk.CTkFrame(parent, fg_color="#1E293B", corner_radius=12, border_width=1, border_color="#334155")
        gate_box.pack(fill="x", padx=14, pady=(14, 10))

        gb_head = ctk.CTkFrame(gate_box, fg_color="transparent")
        gb_head.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(gb_head, text="BARRIER GATE", font=ctk.CTkFont(size=13, weight="bold"), text_color="#F8FAFC").pack(side="left")

        self.gate_badge = ctk.CTkLabel(gb_head, text="CLOSED", font=ctk.CTkFont(size=11, weight="bold"), text_color="#EF4444")
        self.gate_badge.pack(side="right")

        self.gate_canvas = tk.Canvas(gate_box, width=300, height=75, bg="#0F172A", highlightthickness=0)
        self.gate_canvas.pack(padx=14, pady=6)
        self._draw_gate_canvas(is_open=False)

        g_btns = ctk.CTkFrame(gate_box, fg_color="transparent")
        g_btns.pack(fill="x", padx=14, pady=(6, 12))

        ctk.CTkButton(g_btns, text="Open Gate", height=32, corner_radius=8, font=ctk.CTkFont(size=11, weight="bold"), fg_color="#10B981", hover_color="#059669", command=lambda: self.serial_service.send_command("CMD:GATE:OPEN")).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(g_btns, text="Close Gate", height=32, corner_radius=8, font=ctk.CTkFont(size=11, weight="bold"), fg_color="#EF4444", hover_color="#DC2626", command=lambda: self.serial_service.send_command("CMD:GATE:CLOSE")).pack(side="right", fill="x", expand=True, padx=(4, 0))

        rfid_box = ctk.CTkFrame(parent, fg_color="#1E293B", corner_radius=12, border_width=1, border_color="#334155")
        rfid_box.pack(fill="x", padx=14, pady=6)

        rf_head = ctk.CTkFrame(rfid_box, fg_color="transparent")
        rf_head.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(rf_head, text="SCANNED RFID CARD", font=ctk.CTkFont(size=13, weight="bold"), text_color="#F8FAFC").pack(side="left")
        self.rfid_time_lbl = ctk.CTkLabel(rf_head, text="", font=ctk.CTkFont(size=11), text_color="#64748B")
        self.rfid_time_lbl.pack(side="right")

        face = ctk.CTkFrame(rfid_box, fg_color="#0F172A", corner_radius=10, border_width=1, border_color="#334155")
        face.pack(fill="x", padx=14, pady=(2, 8))
        self.rfid_card_lbl = ctk.CTkLabel(face, text="— — — —", font=ctk.CTkFont(family="Courier", size=20, weight="bold"), text_color="#64748B")
        self.rfid_card_lbl.pack(pady=(10, 0))
        self.rfid_status_lbl = ctk.CTkLabel(face, text="Waiting for card scan…", font=ctk.CTkFont(size=12, weight="bold"), text_color="#94A3B8")
        self.rfid_status_lbl.pack(pady=(0, 10))

        ctk.CTkLabel(rfid_box, text="Recent scans", font=ctk.CTkFont(size=11), text_color="#64748B").pack(anchor="w", padx=14)
        history = ctk.CTkFrame(rfid_box, fg_color="transparent")
        history.pack(fill="x", padx=14, pady=(0, 10))
        self.rfid_history_lbls = []
        for _ in range(3):
            lbl = ctk.CTkLabel(history, text="", height=18, font=ctk.CTkFont(family="Courier", size=11), text_color="#64748B")
            lbl.pack(anchor="w")
            self.rfid_history_lbls.append(lbl)

        gauge_box = ctk.CTkFrame(parent, fg_color="#1E293B", corner_radius=12, border_width=1, border_color="#334155")
        gauge_box.pack(fill="both", expand=True, padx=14, pady=(6, 14))

        g_head = ctk.CTkFrame(gauge_box, fg_color="transparent")
        g_head.pack(fill="x", padx=14, pady=(12, 6))
        ctk.CTkLabel(g_head, text="PARKING LOT CAPACITY", font=ctk.CTkFont(size=12, weight="bold"), text_color="#F8FAFC").pack(side="left")
        self.gauge_pct_lbl = ctk.CTkLabel(g_head, text="0% FULL", font=ctk.CTkFont(size=11, weight="bold"), text_color="#34D399")
        self.gauge_pct_lbl.pack(side="right")

        self.progress_bar = ctk.CTkProgressBar(gauge_box, height=12, corner_radius=6, progress_color="#10B981")
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", padx=14, pady=(4, 12))

        legend = ctk.CTkFrame(gauge_box, fg_color="#0F172A", corner_radius=8)
        legend.pack(fill="x", padx=14, pady=(0, 10))

        r1 = ctk.CTkFrame(legend, fg_color="transparent")
        r1.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(r1, text="🟢 Available for Booking", font=ctk.CTkFont(size=11), text_color="#10B981").pack(side="left")
        self.leg_free = ctk.CTkLabel(r1, text="4 Slots", font=ctk.CTkFont(size=11, weight="bold"), text_color="#F8FAFC")
        self.leg_free.pack(side="right")

        r2 = ctk.CTkFrame(legend, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(r2, text="🔴 Occupied Vehicles", font=ctk.CTkFont(size=11), text_color="#EF4444").pack(side="left")
        self.leg_occ = ctk.CTkLabel(r2, text="0 Slots", font=ctk.CTkFont(size=11, weight="bold"), text_color="#F8FAFC")
        self.leg_occ.pack(side="right")

        r3 = ctk.CTkFrame(legend, fg_color="transparent")
        r3.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(r3, text="🟡 Reserved Bookings", font=ctk.CTkFont(size=11), text_color="#F59E0B").pack(side="left")
        self.leg_bk = ctk.CTkLabel(r3, text="0 Slots", font=ctk.CTkFont(size=11, weight="bold"), text_color="#F8FAFC")
        self.leg_bk.pack(side="right")

    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color="#0F172A", height=28, corner_radius=0)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)

        self.conn_status_lbl = ctk.CTkLabel(footer, text="● Disconnected. Select port and click Connect.", font=ctk.CTkFont(size=11), text_color="#64748B")
        self.conn_status_lbl.pack(side="left", padx=20)

        ctk.CTkLabel(footer, text="Smart Car Parking System", font=ctk.CTkFont(size=11), text_color="#475569").pack(side="right", padx=20)

    def _draw_gate_canvas(self, is_open: bool):
        self.gate_canvas.delete("all")
        w = 300
        h = 75

        self.gate_canvas.create_rectangle(0, h - 6, w, h, fill="#334155", outline="")

        px1, py1, px2, py2 = 25, 24, 48, h - 6
        self.gate_canvas.create_rectangle(px1, py1, px2, py2, fill="#1E293B", outline="#475569", width=2)
        self.gate_canvas.create_rectangle(px1 - 2, py1 - 3, px2 + 2, py1 + 2, fill="#F59E0B", outline="")

        piv_x = 36
        piv_y = 34
        self.gate_canvas.create_oval(piv_x - 5, piv_y - 5, piv_x + 5, piv_y + 5, fill="#CBD5E1", outline="#000000")

        if is_open:
            self.gate_canvas.create_line(piv_x, piv_y, piv_x + 30, 8, fill="#10B981", width=6)
            self.gate_canvas.create_text(w - 65, 34, text="GATE OPEN", font=("Helvetica", 12, "bold"), fill="#10B981")
            self.gate_badge.configure(text="OPEN", text_color="#10B981")
        else:
            self.gate_canvas.create_line(piv_x, piv_y, w - 35, piv_y, fill="#EF4444", width=6)
            self.gate_canvas.create_line(piv_x, piv_y, w - 35, piv_y, fill="#FFFFFF", width=2, dash=(6, 6))
            self.gate_canvas.create_text(w - 65, 34, text="GATE CLOSED", font=("Helvetica", 12, "bold"), fill="#EF4444")
            self.gate_badge.configure(text="CLOSED", text_color="#EF4444")

    def _refresh_ports(self):
        ports = self.serial_service.get_available_ports()
        self.port_combo.configure(values=ports)
        if ports and not any(x in ports[0] for x in ["No Device", "No USB", "No Serial"]):
            self.port_combo.set(ports[0])
        else:
            self.port_combo.set(ports[0] if ports else "No Device Detected")

    def _toggle_connection(self):
        if self.serial_service.is_connected:
            self.serial_service.disconnect()
        else:
            port = self.port_combo.get()
            baud = int(self.baud_combo.get())
            if not port or any(x in port for x in ["No Device", "No USB", "No Serial", "No Port"]):
                messagebox.showwarning("No Device Detected", "Please plug in the device and select its port from the dropdown.")
                return

            self.connect_btn.configure(text="Connecting...", fg_color="#F59E0B", state="disabled")
            self.conn_status_lbl.configure(text=f"Connecting to {port} in background...", text_color="#F59E0B")
            self.serial_service.connect_async(port, baud)

    def _on_conn_changed(self, connected: bool, message: str):
        self.after(0, lambda: self._apply_conn_ui(connected, message))

    def _apply_conn_ui(self, connected: bool, message: str):
        if connected:
            self.connect_btn.configure(text="Disconnect", fg_color="#EF4444", hover_color="#DC2626", state="normal")
            self.conn_status_lbl.configure(text=f"● {message}", text_color="#10B981")
        else:
            self.connect_btn.configure(text="Connect", fg_color="#10B981", hover_color="#059669", state="normal")
            self.conn_status_lbl.configure(text=f"● {message}", text_color="#64748B")

    def _on_book_slot(self, slot_num: int):
        idx = slot_num - 1
        card = self.slot_cards[idx]

        if card.current_state == "OCCUPIED":
            messagebox.showerror("Slot Occupied", f"Slot {slot_num} is currently full with a vehicle. Please select a green available slot.")
            return

        if card.current_state == "BOOKED":
            messagebox.showinfo("Already Booked", f"Slot {slot_num} is already booked. You can cancel it using the amber button.")
            return

        BookingDialog(self, slot_num=slot_num, on_confirmed=self._handle_booking_confirmed)

    def _handle_booking_confirmed(self, slot_num: int, plate: str, name: str, phone: str):
        now = datetime.now()
        info = {
            "ticket": now.strftime("PK-%y%m%d%H%M%S") + f"-S{slot_num}",
            "plate": plate,
            "name": name,
            "phone": phone,
            "time": now.strftime("%I:%M %p, %d %b %Y")
        }
        self.bookings[slot_num] = info
        self._log_booking("BOOKED", slot_num, info)

        self.serial_service.send_command(f"CMD:BOOK:{slot_num}")
        self.slot_cards[slot_num - 1].update_state(is_occupied=False, is_booked=True, booking_info=info)
        self._update_stats()

        BookingSuccessModal(self, slot_num=slot_num, info=info)

    def _on_release_slot(self, slot_num: int):
        confirm = messagebox.askyesno("Release Slot", f"Are you sure you want to cancel the reservation for Slot {slot_num}?")
        if confirm:
            info = self.bookings.pop(slot_num, None)
            if info:
                self._log_booking("CANCELLED", slot_num, info)

            self.serial_service.send_command(f"CMD:RELEASE:{slot_num}")
            self.slot_cards[slot_num - 1].update_state(is_occupied=False, is_booked=False)
            self._update_stats()

    def _log_booking(self, event: str, slot_num: int, info: Dict):
        try:
            log_booking(event, slot_num, info)
        except OSError as e:
            messagebox.showwarning("Booking Log", f"Could not write {BOOKINGS_CSV}\n(close it if open in Excel)\n\n{e}")

    def _on_book_failed(self, slot_num: int):
        self.after(0, lambda: self._apply_book_failed(slot_num))

    def _apply_book_failed(self, slot_num: int):
        info = self.bookings.pop(slot_num, None)
        if not info:
            return
        self._log_booking("REJECTED", slot_num, info)
        messagebox.showerror("Booking Failed", f"Slot {slot_num} is occupied — booking for {info['plate']} was not accepted by the controller.")

    def _on_status_received(self, status: Dict):
        self.after(0, lambda: self._apply_status_data(status))

    def _apply_status_data(self, status: Dict):
        # GATE:OPEN/CLOSED lines arrive as {"GATE": x} only; don't treat missing slot keys as "free"
        if "S1" in status:
            for i in range(4):
                slot_num = i + 1
                is_occupied = status.get(f"S{slot_num}") == 1
                arduino_booked = status.get(f"B{slot_num}") == 1
                info = self.bookings.get(slot_num)

                if is_occupied and info:
                    self._log_booking("PARKED", slot_num, info)
                    del self.bookings[slot_num]
                    info = None
                elif info and not arduino_booked:
                    # Arduino missed the booking (sent during its reset, dropped byte, power glitch): resend.
                    # CMD:BOOK is idempotent on the Arduino, so a duplicate is harmless.
                    self.serial_service.send_command(f"CMD:BOOK:{slot_num}")

                self.slot_cards[i].update_state(is_occupied=is_occupied, is_booked=arduino_booked or info is not None, booking_info=info)

        if "GATE" in status:
            self._draw_gate_canvas(status["GATE"] == 1)

        self._update_stats()

    def _on_rfid_event(self, card_id: str, auth_status: str):
        self.after(0, lambda: self._apply_rfid_event(card_id, auth_status))

    def _apply_rfid_event(self, card_id: str, auth_status: str):
        granted = auth_status == "AUTHORIZED"
        color = "#10B981" if granted else "#EF4444"
        now_str = datetime.now().strftime("%H:%M:%S")

        self.rfid_card_lbl.configure(text=card_id, text_color=color)
        self.rfid_status_lbl.configure(text="✓ ACCESS GRANTED" if granted else "✗ ACCESS DENIED", text_color=color)
        self.rfid_time_lbl.configure(text=now_str)
        if granted:
            self._draw_gate_canvas(is_open=True)

        self.rfid_history.insert(0, f"{now_str}  {card_id:<12}  {'OK' if granted else 'DENIED'}")
        del self.rfid_history[3:]
        for i, lbl in enumerate(self.rfid_history_lbls):
            lbl.configure(text=self.rfid_history[i] if i < len(self.rfid_history) else "")

    def _update_stats(self):
        total = 4
        occupied = sum(1 for c in self.slot_cards if c.current_state == "OCCUPIED")
        booked = sum(1 for c in self.slot_cards if c.current_state == "BOOKED")
        free = max(0, total - occupied - booked)

        self.metric_cards["TOTAL SLOTS"].configure(text=str(total))
        self.metric_cards["AVAILABLE SLOTS"].configure(text=str(free))
        self.metric_cards["OCCUPIED (FULL)"].configure(text=str(occupied))
        self.metric_cards["BOOKED / RESERVED"].configure(text=str(booked))

        used = occupied + booked
        pct = (used / total)
        self.progress_bar.set(pct)
        pct_display = int(pct * 100)
        self.gauge_pct_lbl.configure(
            text=f"{pct_display}% FULL",
            text_color="#EF4444" if pct_display >= 75 else ("#F59E0B" if pct_display >= 50 else "#10B981")
        )
        self.progress_bar.configure(
            progress_color="#EF4444" if pct_display >= 75 else ("#F59E0B" if pct_display >= 50 else "#10B981")
        )
        self.leg_free.configure(text=f"{free} Slots")
        self.leg_occ.configure(text=f"{occupied} Slots")
        self.leg_bk.configure(text=f"{booked} Slots")


if __name__ == "__main__":
    app = ParkingApp()
    app.mainloop()
