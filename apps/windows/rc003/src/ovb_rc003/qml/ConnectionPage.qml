// Connection and audio-routing settings. Business rules stay in
// SettingsController; this page groups the existing controls in the order
// users encounter them: device/status, audio route, voice control, then
// runtime/startup. The final action remains visible below the scrolling area.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens
    property int contentMaximumWidth: 1120

    Dialog {
        id: voiceHotkeyRecorder
        objectName: "voiceHotkeyRecorderDialog"
        modal: true
        anchors.centerIn: parent
        width: 430
        title: qsTr("录制语音快捷键")
        standardButtons: Dialog.Cancel
        property string previewText: qsTr("请按下要使用的键盘组合")
        property bool recordsSecondary: false

        function commitShortcut(chord) {
            previewText = chord
            if (recordsSecondary)
                SettingsController.secondaryHotkeyText = chord
            else
                SettingsController.hotkeyText = chord
            close()
        }

        onOpened: {
            previewText = qsTr("请按下要使用的键盘组合")
            voiceHotkeyCaptureArea.forceActiveFocus()
            SettingsController.startHotkeyCapture()
        }
        onClosed: SettingsController.stopHotkeyCapture()

        Connections {
            target: SettingsController
            function onHotkeyCaptured(chord) {
                if (voiceHotkeyRecorder.visible)
                    voiceHotkeyRecorder.commitShortcut(chord)
            }
            function onHotkeyCaptureError(message) {
                if (voiceHotkeyRecorder.visible)
                    voiceHotkeyRecorder.previewText = message
            }
        }

        contentItem: FocusScope {
            id: voiceHotkeyCaptureArea
            implicitHeight: 150
            focus: true

            ColumnLayout {
                anchors.fill: parent
                spacing: tokens.spacingMedium
                Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: voiceHotkeyRecorder.previewText
                    font.pixelSize: tokens.fontSizeTitle
                    color: tokens.accent
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    horizontalAlignment: Text.AlignHCenter
                    text: qsTr("请直接按下键盘快捷键；左右修饰键会分别记录。录制期间不会执行该快捷键。")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

            ColumnLayout {
                width: Math.min(root.width - tokens.spacingLarge * 2,
                                root.contentMaximumWidth)
                x: Math.max(tokens.spacingLarge, (root.width - width) / 2)
                y: tokens.spacingMedium
                spacing: tokens.spacingMedium

                // -- Device and truthful process status --------------------
                Rectangle {
                    Layout.fillWidth: true
                    radius: tokens.cornerRadiusLarge
                    color: tokens.surface
                    border.color: tokens.border
                    border.width: 1
                    implicitHeight: deviceColumn.implicitHeight + tokens.spacingMedium * 2

                    Timer {
                        interval: 1000
                        repeat: true
                        running: parent.visible && SettingsController.isRc003Device
                        onTriggered: SettingsController.refreshBridgeStatus()
                    }

                    ColumnLayout {
                        id: deviceColumn
                        anchors.fill: parent
                        anchors.margins: tokens.spacingMedium
                        spacing: tokens.spacingSmall

                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                text: qsTr("设备与运行状态")
                                font.pixelSize: tokens.fontSizeTitle
                                font.bold: true
                                color: tokens.textPrimary
                            }
                            Item { Layout.fillWidth: true }
                            Rectangle {
                                visible: SettingsController.isRc003Device
                                width: 8
                                height: 8
                                radius: 4
                                color: SettingsController.bridgeRunning
                                       ? tokens.successColor : tokens.disabledText
                            }
                            Label {
                                visible: SettingsController.isRc003Device
                                text: SettingsController.bridgeRunning
                                      ? qsTr("桥接运行中") : qsTr("桥接未运行")
                                color: SettingsController.bridgeRunning
                                       ? tokens.successColor : tokens.textSecondary
                                font.pixelSize: tokens.fontSizeBody
                            }
                        }

                        ComboBox {
                            id: deviceCombo
                            objectName: "deviceCombo"
                            Layout.fillWidth: true
                            model: SettingsController.deviceOptions
                            currentIndex: SettingsController.selectedDeviceIndex
                            onActivated: SettingsController.selectedDeviceIndex = index
                            enabled: SettingsController.deviceCatalogAvailable
                            Accessible.name: qsTr("当前设备")
                        }
                        Label {
                            visible: !SettingsController.deviceCatalogAvailable
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            text: SettingsController.deviceCatalogErrorText
                            color: tokens.errorColor
                            font.pixelSize: tokens.fontSizeSmall
                        }
                        Label {
                            visible: SettingsController.isRc003Device
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            text: qsTr("桥接运行只表示后台进程已启动；遥控器连接、按键和语音状态可在“检查与修复”中确认。")
                            color: tokens.textSecondary
                            font.pixelSize: tokens.fontSizeSmall
                        }
                    }
                }

                // -- One coherent audio route -----------------------------
                Rectangle {
                    visible: SettingsController.isRc003Device
                    Layout.fillWidth: true
                    radius: tokens.cornerRadiusLarge
                    color: tokens.surface
                    border.color: tokens.border
                    border.width: 1
                    implicitHeight: audioRouteColumn.implicitHeight + tokens.spacingMedium * 2

                    Timer {
                        interval: 1000
                        repeat: true
                        running: parent.visible
                        onTriggered: SettingsController.refreshUnifiedAudioStatus()
                    }

                    ColumnLayout {
                        id: audioRouteColumn
                        anchors.fill: parent
                        anchors.margins: tokens.spacingMedium
                        spacing: tokens.spacingSmall

                        Label {
                            text: qsTr("虚拟音频桥接")
                            font.pixelSize: tokens.fontSizeTitle
                            font.bold: true
                            color: tokens.textPrimary
                        }

                        Label {
                            text: qsTr("桥接到")
                            color: tokens.textPrimary
                            font.pixelSize: tokens.fontSizeBody
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            ComboBox {
                                id: endpointCombo
                                objectName: "endpointCombo"
                                Layout.fillWidth: true
                                model: SettingsController.endpointOptions
                                currentIndex: SettingsController.selectedEndpointIndex
                                onActivated: SettingsController.selectedEndpointIndex = index
                                Accessible.name: qsTr("桥接到")
                            }
                            Button {
                                id: showAllAudioEndpointsButton
                                objectName: "showAllAudioEndpointsButton"
                                text: SettingsController.showAllAudioEndpoints
                                      ? qsTr("仅显示虚拟设备") : qsTr("显示全部设备")
                                onClicked: SettingsController.showAllAudioEndpoints
                                           = !SettingsController.showAllAudioEndpoints
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            text: SettingsController.endpointBridgeHelpText
                            color: tokens.textSecondary
                            font.pixelSize: tokens.fontSizeSmall
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.topMargin: tokens.spacingTiny
                            Layout.bottomMargin: tokens.spacingTiny
                            height: 1
                            color: tokens.border
                        }

                        CheckBox {
                            id: unifiedInputCheck
                            objectName: "unifiedInputCheck"
                            text: qsTr("同时接入电脑麦克风（遥控器优先）")
                            checked: SettingsController.unifiedVirtualInputEnabled
                            onToggled: SettingsController.unifiedVirtualInputEnabled = checked
                            Accessible.name: text
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.leftMargin: tokens.spacingLarge + tokens.spacingSmall
                            spacing: tokens.spacingSmall
                            enabled: SettingsController.unifiedVirtualInputEnabled
                            opacity: enabled ? 1.0 : 0.48

                            Label {
                                text: qsTr("系统麦克风")
                                color: tokens.textPrimary
                                font.pixelSize: tokens.fontSizeBody
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ComboBox {
                                    id: systemInputCombo
                                    objectName: "systemInputCombo"
                                    Layout.fillWidth: true
                                    model: SettingsController.systemInputOptions
                                    currentIndex: SettingsController.selectedSystemInputIndex
                                    onActivated: SettingsController.selectedSystemInputIndex = index
                                    Accessible.name: qsTr("系统麦克风设备")
                                }
                                Button {
                                    text: qsTr("刷新麦克风")
                                    onClicked: SettingsController.refreshSystemInputOptions()
                                }
                            }
                            CheckBox {
                                id: unifiedOnDemandCheck
                                objectName: "unifiedOnDemandCheck"
                                text: SettingsController.selectedEndpointSupportsOnDemand
                                      ? qsTr("没有软件使用时，关闭电脑麦克风")
                                      : qsTr("空闲检测仅支持 VB-CABLE")
                                enabled: SettingsController.selectedEndpointSupportsOnDemand
                                checked: SettingsController.unifiedOnDemandEnabled
                                onToggled: SettingsController.unifiedOnDemandEnabled = checked
                                Accessible.name: text
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: qsTr("系统麦克风透明转发；RC003 说话时暂停系统麦克风，结束或异常后自动恢复，不做降噪、变声或混音。")
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                        }

                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            text: SettingsController.unifiedAudioStatusText
                            color: SettingsController.unifiedAudioStatusText.indexOf("错误：") === 0
                                   ? tokens.errorColor : tokens.textSecondary
                            font.pixelSize: tokens.fontSizeBody
                        }
                    }
                }

                // -- Voice behavior, in decision order ---------------------
                Rectangle {
                    visible: SettingsController.isRc003Device
                    Layout.fillWidth: true
                    radius: tokens.cornerRadiusLarge
                    color: tokens.surface
                    border.color: tokens.border
                    border.width: 1
                    implicitHeight: voiceControlColumn.implicitHeight + tokens.spacingMedium * 2

                    ColumnLayout {
                        id: voiceControlColumn
                        anchors.fill: parent
                        anchors.margins: tokens.spacingMedium
                        spacing: tokens.spacingSmall

                        Label {
                            text: qsTr("遥控器语音控制")
                            font.pixelSize: tokens.fontSizeTitle
                            font.bold: true
                            color: tokens.textPrimary
                        }

                        GridLayout {
                            Layout.fillWidth: true
                            columns: 2
                            columnSpacing: tokens.spacingMedium
                            rowSpacing: tokens.spacingSmall

                            Label {
                                text: qsTr("触发方式")
                                color: tokens.textPrimary
                            }
                            ComboBox {
                                id: triggerModeCombo
                                objectName: "triggerModeCombo"
                                Layout.fillWidth: true
                                model: SettingsController.triggerModeOptions
                                currentIndex: SettingsController.triggerModeIndex
                                onActivated: SettingsController.triggerModeIndex = index
                                Accessible.name: qsTr("语音触发方式")
                            }

                            Label {
                                Layout.columnSpan: 2
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: qsTr("RC003 不支持点按后持续录音，需要按住麦克风键才能持续传送声音。这里的点按或按住仅指电脑端快捷键。")
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                            }

                            Label {
                                text: qsTr("语音快捷键")
                                color: tokens.textPrimary
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                TextField {
                                    id: hotkeyField
                                    objectName: "hotkeyField"
                                    Layout.fillWidth: true
                                    text: SettingsController.hotkeyText
                                    placeholderText: qsTr("例如：ralt")
                                    selectByMouse: true
                                    onEditingFinished: SettingsController.hotkeyText = text
                                    Accessible.name: qsTr("语音快捷键")
                                }
                                Button {
                                    id: recordVoiceHotkeyButton
                                    objectName: "recordVoiceHotkeyButton"
                                    text: qsTr("录制")
                                    onClicked: {
                                        voiceHotkeyRecorder.recordsSecondary = false
                                        voiceHotkeyRecorder.open()
                                    }
                                    Accessible.name: qsTr("录制语音快捷键")
                                }
                            }

                            Connections {
                                target: SettingsController
                                function onHotkeyTextChanged() {
                                    hotkeyField.text = SettingsController.hotkeyText
                                }
                            }

                            Label {
                                text: qsTr("第二语音快捷键")
                                color: tokens.textPrimary
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                TextField {
                                    id: secondaryHotkeyField
                                    objectName: "secondaryHotkeyField"
                                    Layout.fillWidth: true
                                    text: SettingsController.secondaryHotkeyText
                                    placeholderText: qsTr("例如：ralt+space；留空则关闭")
                                    selectByMouse: true
                                    onEditingFinished: SettingsController.secondaryHotkeyText = text
                                    Accessible.name: qsTr("第二语音快捷键")
                                }
                                Button {
                                    id: recordSecondaryVoiceHotkeyButton
                                    objectName: "recordSecondaryVoiceHotkeyButton"
                                    text: qsTr("录制")
                                    onClicked: {
                                        voiceHotkeyRecorder.recordsSecondary = true
                                        voiceHotkeyRecorder.open()
                                    }
                                    Accessible.name: qsTr("录制第二语音快捷键")
                                }
                            }

                            Label { text: qsTr("第二按键方式"); color: tokens.textPrimary }
                            CheckBox {
                                id: secondaryGestureCheck
                                objectName: "secondaryGestureCheck"
                                text: qsTr("启用短按后再次长按")
                                checked: SettingsController.secondaryGestureEnabled
                                onToggled: SettingsController.secondaryGestureEnabled = checked
                                Accessible.name: qsTr("启用麦克风键短按后再次长按")
                            }

                            Connections {
                                target: SettingsController
                                function onSecondaryHotkeyTextChanged() {
                                    secondaryHotkeyField.text = SettingsController.secondaryHotkeyText
                                }
                            }

                            Label {
                                Layout.columnSpan: 2
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: qsTr("直接按住使用第一快捷键；短按一次后再次按住使用第二快捷键。两个快捷键都可以修改；取消上方勾选即可关闭第二按键方式。")
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                            }

                            Label {
                                Layout.columnSpan: 2
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: triggerModeCombo.currentIndex === 0
                                      ? qsTr("针对 RC003 的松键时序和防抖优化；当前 Typeless 推荐使用 ralt，也可以手动修改。")
                                      : triggerModeCombo.currentIndex === 1
                                        ? qsTr("开始和结束时分别完整点按一次上方快捷键。")
                                        : qsTr("说话期间持续按住上方快捷键，语音结束后释放。")
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                        }
                    }
                }

                // -- DJI Mic 2 system-input workflow -----------------------
                Rectangle {
                    visible: SettingsController.isDjiMic2Device
                    Layout.fillWidth: true
                    radius: tokens.cornerRadiusLarge
                    color: tokens.surface
                    border.color: tokens.border
                    border.width: 1
                    implicitHeight: djiInputColumn.implicitHeight + tokens.spacingMedium * 2

                    ColumnLayout {
                        id: djiInputColumn
                        anchors.fill: parent
                        anchors.margins: tokens.spacingMedium
                        spacing: tokens.spacingSmall

                        Label {
                            text: qsTr("DJI Mic 2 录音输入")
                            font.pixelSize: tokens.fontSizeTitle
                            font.bold: true
                            color: tokens.textPrimary
                        }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            text: SettingsController.djiMicStatusText
                            color: tokens.textSecondary
                            font.pixelSize: tokens.fontSizeBody
                        }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            text: qsTr("DJI Mic 2 使用 Windows 系统录音输入，不使用 RC003 的虚拟音频输出或桥接进程。")
                            color: tokens.textSecondary
                            font.pixelSize: tokens.fontSizeSmall
                        }
                        RowLayout {
                            spacing: tokens.spacingSmall
                            Button {
                                text: qsTr("重新检测")
                                onClicked: SettingsController.refreshDjiMicStatus()
                            }
                            Button {
                                text: qsTr("打开 Windows 声音输入设置")
                                highlighted: true
                                onClicked: SettingsController.openSoundSettings()
                            }
                        }
                    }
                }

                // -- Runtime and startup; saving is handled by the footer --
                Rectangle {
                    visible: SettingsController.isRc003Device
                    Layout.fillWidth: true
                    radius: tokens.cornerRadiusLarge
                    color: tokens.surface
                    border.color: tokens.border
                    border.width: 1
                    implicitHeight: runtimeColumn.implicitHeight + tokens.spacingMedium * 2

                    ColumnLayout {
                        id: runtimeColumn
                        anchors.fill: parent
                        anchors.margins: tokens.spacingMedium
                        spacing: tokens.spacingSmall

                        Label {
                            text: qsTr("运行与启动")
                            font.pixelSize: tokens.fontSizeTitle
                            font.bold: true
                            color: tokens.textPrimary
                        }
                        CheckBox {
                            id: autostartCheck
                            objectName: "autostartCheck"
                            text: qsTr("登录 Windows 时自动启动 Remote Mic 桥接")
                            checked: SettingsController.autostartEnabled
                            onToggled: SettingsController.setAutostartEnabled(checked)
                            Accessible.name: text
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                Layout.fillWidth: true
                                text: SettingsController.bridgeRunning
                                      ? qsTr("后台桥接正在运行。") : qsTr("后台桥接当前未运行。")
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeBody
                            }
                            Button {
                                id: openLogButton
                                objectName: "openLogButton"
                                text: qsTr("打开日志目录")
                                onClicked: SettingsController.openLogLocation()
                            }
                        }
                    }
                }

                Item { Layout.preferredHeight: tokens.spacingTiny }
            }
        }

        // One action bar applies the entire page and remains visible while
        // the cards scroll. Save feedback can no longer float between cards.
        Rectangle {
            id: connectionActionBar
            objectName: "connectionActionBar"
            Layout.fillWidth: true
            implicitHeight: actionBarRow.implicitHeight + tokens.spacingMedium * 2
            color: tokens.background
            border.color: tokens.border
            border.width: 1

            RowLayout {
                id: actionBarRow
                width: Math.min(parent.width - tokens.spacingLarge * 2,
                                root.contentMaximumWidth)
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.verticalCenter: parent.verticalCenter
                spacing: tokens.spacingSmall

                Label {
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                    text: SettingsController.errorMessage.length > 0
                          ? SettingsController.errorMessage
                          : SettingsController.statusMessage.length > 0
                            ? SettingsController.statusMessage
                            : qsTr("修改设置后，保存以应用。")
                    color: SettingsController.errorMessage.length > 0
                           ? tokens.errorColor
                           : SettingsController.statusMessage.length > 0
                             ? tokens.successColor : tokens.textSecondary
                    font.pixelSize: tokens.fontSizeBody
                }
                Button {
                    id: connectionSaveButton
                    objectName: "connectionSaveButton"
                    text: SettingsController.isRc003Device
                          ? SettingsController.bridgeActionText : qsTr("保存设备选择")
                    highlighted: true
                    onClicked: {
                        if (SettingsController.isRc003Device)
                            SettingsController.saveAndLaunch()
                        else
                            SettingsController.saveSettings()
                    }
                    KeyNavigation.tab: openLogButton
                }
            }
        }
    }
}
