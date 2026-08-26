// "按键" tab (XRBM-030 In-scope items 4/5): the RC003 product photo with 13
// clickable hotspots calibrated for the bundled RC003 photo (see
// remote_layout.py) on
// the left, the compact two-column mapping grid (Chinese name / HID usage /
// current action) on the right. Both sides are two views over the SAME ButtonMappingModel row,
// kept in sync through SettingsController.selectButton()/
// SettingsController.selectedButtonId - clicking either one updates both
// (In-scope item 4's "双向定位"). SettingsController/ButtonMappingModel are
// QML singletons - see main.qml's module docstring for why.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens

    Timer {
        interval: 1000
        repeat: true
        running: root.visible
        onTriggered: SettingsController.refreshBridgeStatus()
    }

    readonly property real photoAspectRatio: 1030 / 508
    readonly property string textSubmitPreset: qsTr("输入文本…")
    readonly property string textSubmitPrefix: qsTr("输入文本：")
    readonly property string legacyTextSubmitPrefix: qsTr("输入文本并回车：")

    function isActionCategory(value) {
        return (value || "").indexOf("── ") === 0
    }

    function activeProfileName() {
        var names = SettingsController.mappingProfileNames
        var index = SettingsController.activeMappingProfileIndex
        return index >= 0 && index < names.length ? names[index] : ""
    }

    function openShortcutRecorder(buttonId, rowIndex, isMic, trigger) {
        shortcutRecorder.buttonId = buttonId
        shortcutRecorder.rowIndex = rowIndex
        shortcutRecorder.isMic = isMic
        shortcutRecorder.trigger = trigger || "single_click"
        shortcutRecorder.previewText = qsTr("请按下要映射的真实按键")
        shortcutRecorder.open()
    }

    function isTextSubmitAction(actionText) {
        var value = actionText || ""
        return value === textSubmitPreset
            || value === qsTr("输入自定义文本并回车…")
            || value === qsTr("输入“执行”并回车")
            || value.indexOf(textSubmitPrefix) === 0
            || value.indexOf(legacyTextSubmitPrefix) === 0
    }

    function textSubmitPayload(actionText) {
        var value = actionText || ""
        if (value.indexOf(textSubmitPrefix) === 0)
            return value.substring(textSubmitPrefix.length)
        if (value.indexOf(legacyTextSubmitPrefix) === 0)
            return value.substring(legacyTextSubmitPrefix.length)
        return qsTr("执行")
    }

    function openTextSubmitEditor(rowIndex, trigger, actionText) {
        textSubmitEditor.rowIndex = rowIndex
        textSubmitEditor.trigger = trigger || "single_click"
        textSubmitEditor.previousActionText = actionText || textSubmitPreset
        textSubmitField.text = textSubmitPayload(actionText)
        textSubmitEditor.open()
    }

    Dialog {
        id: shortcutRecorder
        objectName: "shortcutRecorderDialog"
        modal: true
        anchors.centerIn: parent
        width: 430
        title: qsTr("录制自定义快捷键")
        standardButtons: Dialog.Cancel
        property string buttonId: ""
        property int rowIndex: -1
        property bool isMic: false
        property string trigger: "single_click"
        property string previewText: ""

        function commitShortcut(chord) {
            previewText = chord
            if (isMic)
                SettingsController.hotkeyText = chord
            else if (trigger === "single_click")
                ButtonMappingModel.setActionTextAt(rowIndex, chord)
            else
                ButtonMappingModel.setSecondaryActionTextAt(rowIndex, trigger, chord)
            close()
        }

        onOpened: {
            captureArea.forceActiveFocus()
            SettingsController.startHotkeyCapture()
        }

        onClosed: SettingsController.stopHotkeyCapture()

        Connections {
            target: SettingsController
            function onHotkeyCaptured(chord) {
                if (shortcutRecorder.visible)
                    shortcutRecorder.commitShortcut(chord)
            }
            function onHotkeyCaptureError(message) {
                if (shortcutRecorder.visible)
                    shortcutRecorder.previewText = message
            }
        }

        contentItem: FocusScope {
            id: captureArea
            implicitHeight: 150
            focus: true

            ColumnLayout {
                anchors.fill: parent
                spacing: tokens.spacingMedium
                Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: shortcutRecorder.previewText
                    font.pixelSize: tokens.fontSizeTitle
                    color: tokens.accent
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    horizontalAlignment: Text.AlignHCenter
                    text: qsTr("请直接按下要映射的真实按键；左右修饰键会分别记录。录制期间不会执行该快捷键。")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                }
            }
        }
    }

    Dialog {
        id: textSubmitEditor
        objectName: "textSubmitEditorDialog"
        modal: true
        anchors.centerIn: parent
        width: 480
        title: qsTr("编辑要输入的文本")
        standardButtons: Dialog.Ok | Dialog.Cancel
        property int rowIndex: -1
        property string trigger: "single_click"
        property string previousActionText: ""

        onOpened: {
            textSubmitField.forceActiveFocus()
            textSubmitField.selectAll()
        }

        onAccepted: {
            var payload = textSubmitField.text.trim()
            if (payload.length === 0)
                return
            var displayText = root.textSubmitPrefix + payload
            if (trigger === "single_click")
                ButtonMappingModel.setActionTextAt(rowIndex, displayText)
            else
                ButtonMappingModel.setSecondaryActionTextAt(
                    rowIndex, trigger, displayText
                )
        }

        contentItem: ColumnLayout {
            spacing: tokens.spacingSmall

            TextField {
                id: textSubmitField
                objectName: "textSubmitField"
                Layout.fillWidth: true
                maximumLength: 200
                placeholderText: qsTr("例如：执行、继续、请总结上述内容")
                selectByMouse: true
                Accessible.name: qsTr("要输入的文本")
            }

            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("支持中文、英文、数字、标点和 Emoji，最多 200 个字符。触发时只把这一行文字发送到当前输入框，不会自动按回车。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }

            Label {
                Layout.fillWidth: true
                visible: textSubmitField.text.trim().length === 0
                text: qsTr("文本不能为空；如不需要此动作，请在映射中选择“禁用”。")
                color: tokens.errorColor
                font.pixelSize: tokens.fontSizeSmall
            }
        }
    }

    Dialog {
        id: textMenuEditor
        objectName: "textMenuEditorDialog"
        modal: true
        anchors.centerIn: parent
        width: 760
        height: 520
        title: qsTr("编辑文本选择菜单")
        standardButtons: Dialog.Close

        contentItem: ColumnLayout {
            spacing: tokens.spacingSmall

            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("每项可分别设置标题和文本。关闭后点击“保存当前方案”才会写入配置。菜单只输入文字，不会按回车。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }

            ListView {
                id: textMenuList
                objectName: "textMenuItemList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: tokens.spacingTiny
                model: SettingsController.textMenuItems
                ScrollBar.vertical: ScrollBar { }

                delegate: Rectangle {
                    required property int index
                    required property var modelData
                    width: textMenuList.width
                        - (textMenuList.ScrollBar.vertical.visible ? 10 : 0)
                    height: 54
                    radius: tokens.cornerRadiusSmall
                    color: tokens.fieldBackground
                    border.color: tokens.border

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: tokens.spacingTiny
                        spacing: tokens.spacingTiny

                        CheckBox {
                            id: enabledCheck
                            checked: modelData.enabled
                            text: qsTr("启用")
                            onToggled: SettingsController.updateTextMenuItem(
                                index, labelField.text, valueField.text, checked
                            )
                        }
                        TextField {
                            id: labelField
                            Layout.preferredWidth: 130
                            maximumLength: 40
                            text: modelData.label
                            placeholderText: qsTr("标题")
                            onEditingFinished: SettingsController.updateTextMenuItem(
                                index, text, valueField.text, enabledCheck.checked
                            )
                        }
                        TextField {
                            id: valueField
                            Layout.fillWidth: true
                            maximumLength: 200
                            text: modelData.text
                            placeholderText: qsTr("文本")
                            onEditingFinished: SettingsController.updateTextMenuItem(
                                index, labelField.text, text, enabledCheck.checked
                            )
                        }
                        Button {
                            text: qsTr("↑")
                            enabled: index > 0
                            onClicked: SettingsController.moveTextMenuItem(index, -1)
                            Accessible.name: qsTr("上移文本菜单项目")
                        }
                        Button {
                            text: qsTr("↓")
                            enabled: index + 1 < textMenuList.count
                            onClicked: SettingsController.moveTextMenuItem(index, 1)
                            Accessible.name: qsTr("下移文本菜单项目")
                        }
                        Button {
                            text: qsTr("删")
                            onClicked: SettingsController.removeTextMenuItem(index)
                            Accessible.name: qsTr("删除文本菜单项目")
                        }
                    }
                }
            }

            Button {
                text: qsTr("添加菜单项目")
                onClicked: SettingsController.addTextMenuItem(
                    qsTr("新项目"), qsTr("新文本"), true
                )
            }
        }
    }

    Dialog {
        id: profileNameDialog
        objectName: "mappingProfileNameDialog"
        modal: true
        anchors.centerIn: parent
        width: 420
        title: renameMode ? qsTr("重命名配置方案") : qsTr("新建配置方案")
        standardButtons: Dialog.Ok | Dialog.Cancel
        property bool renameMode: false

        function openForCreate() {
            renameMode = false
            profileNameField.text = ""
            open()
        }

        function openForRename() {
            renameMode = true
            profileNameField.text = root.activeProfileName()
            open()
        }

        onOpened: {
            profileNameField.forceActiveFocus()
            profileNameField.selectAll()
        }
        onAccepted: {
            if (renameMode)
                SettingsController.renameActiveMappingProfile(profileNameField.text)
            else
                SettingsController.createMappingProfile(profileNameField.text)
        }

        contentItem: ColumnLayout {
            spacing: tokens.spacingSmall
            TextField {
                id: profileNameField
                objectName: "mappingProfileNameField"
                Layout.fillWidth: true
                maximumLength: 40
                placeholderText: qsTr("方案名称")
                selectByMouse: true
            }
            Label {
                Layout.fillWidth: true
                text: profileNameDialog.renameMode
                    ? qsTr("只修改方案名称，不改变其中的按键映射。")
                    : qsTr("直接复制当前方案的全部映射和快捷文本菜单。")
                wrapMode: Text.WordWrap
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }
        }
    }

    Dialog {
        id: deleteProfileDialog
        objectName: "deleteMappingProfileDialog"
        modal: true
        anchors.centerIn: parent
        width: 420
        title: qsTr("删除配置方案")
        standardButtons: Dialog.Yes | Dialog.Cancel
        onAccepted: SettingsController.deleteActiveMappingProfile()

        contentItem: Label {
            text: qsTr("确定删除“%1”吗？删除后会切换到保留的第一个方案。").arg(
                root.activeProfileName()
            )
            wrapMode: Text.WordWrap
            color: tokens.textPrimary
        }
    }

    RowLayout {
        id: rc003MappingLayout
        objectName: "rc003MappingLayout"
        visible: SettingsController.isRc003Device
        anchors.fill: parent
        anchors.margins: tokens.spacingMedium
        spacing: tokens.spacingMedium

        // -- Left: product photo with hotspots -----------------------------
        ColumnLayout {
            Layout.preferredWidth: 215
            Layout.fillHeight: true
            spacing: tokens.spacingSmall

            Rectangle {
                id: photoFrame
                Layout.preferredWidth: 200
                Layout.preferredHeight: 200 * root.photoAspectRatio
                Layout.alignment: Qt.AlignHCenter | Qt.AlignTop
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                clip: true

                Image {
                    id: photoImage
                    objectName: "photoImage"  // stable hook so a real-QML test can read paintedWidth/paintedHeight and the letterbox offset to verify hotspot centers
                    anchors.fill: parent
                    anchors.margins: 8
                    fillMode: Image.PreserveAspectFit
                    source: SettingsController.photoAvailable ? SettingsController.photoSource : ""
                    visible: SettingsController.photoAvailable
                    smooth: true
                    asynchronous: true
                }

                Label {
                    anchors.centerIn: parent
                    anchors.margins: tokens.spacingMedium
                    width: parent.width - tokens.spacingMedium * 2
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    visible: !SettingsController.photoAvailable
                    text: qsTr("实物图资源缺失")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                }

                Repeater {
                    model: ButtonMappingModel

                    delegate: Item {
                        id: hotspot

                        // Stable, device-identifier-free hook so a real-QML
                        // test can locate any one hotspot Item by button (see
                        // tests/test_qt_settings_app.py). buttonId is an
                        // internal action id ("ok", "power", ...), never a
                        // hardware/BLE identifier.
                        objectName: "photoHotspot_" + buttonId

                        required property string buttonId
                        required property string displayName
                        required property real hotspotX
                        required property real hotspotY
                        required property real hotspotWidth
                        required property real hotspotHeight
                        required property bool isSelected
                        required property bool isVoice

                        readonly property real paintedW: photoImage.paintedWidth
                        readonly property real paintedH: photoImage.paintedHeight
                        readonly property real offsetX: photoImage.x + (photoImage.width - paintedW) / 2
                        readonly property real offsetY: photoImage.y + (photoImage.height - paintedH) / 2

                        // hotspotX/hotspotY are the hotspot's CENTER as a
                        // fraction of the painted photo. A QML Item's x/y are
                        // its TOP-LEFT, so convert center -> top-left by
                        // subtracting half the item's own width/height while
                        // preserving the letterbox offset.
                        width: hotspotWidth * paintedW
                        height: hotspotHeight * paintedH
                        x: offsetX + hotspotX * paintedW - width / 2
                        y: offsetY + hotspotY * paintedH - height / 2
                        visible: SettingsController.photoAvailable

                        activeFocusOnTab: true
                        Accessible.role: Accessible.Button
                        Accessible.name: displayName

                        Rectangle {
                            anchors.fill: parent
                            radius: height / 2
                            color: hotspot.isSelected
                                ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.28)
                                : (hoverHandler.hovered
                                    ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.14)
                                    : "transparent")
                            border.width: hotspot.isSelected ? 2 : (hotspot.activeFocus ? 1 : 0)
                            border.color: hotspot.isVoice ? tokens.voiceAccent : tokens.accent
                        }

                        HoverHandler { id: hoverHandler }
                        TapHandler { onTapped: SettingsController.selectButton(hotspot.buttonId) }
                        Keys.onReturnPressed: SettingsController.selectButton(hotspot.buttonId)
                        Keys.onSpacePressed: SettingsController.selectButton(hotspot.buttonId)
                    }
                }
            }

            Label {
                Layout.preferredWidth: 205
                Layout.alignment: Qt.AlignHCenter
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                text: qsTr("点击实物按键定位映射；普通键可直接输入任意组合键。麦克风键的语音快捷键请在“连接”页设置。遥控器没有独立静音键。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }
        }

            // -- Right: compact reference-style mapping grid -------------------
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: tokens.spacingSmall

            RowLayout {
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: qsTr("按键映射")
                    font.pixelSize: tokens.fontSizeTitle
                    font.bold: true
                    color: tokens.textPrimary
                }
            }

            Rectangle {
                objectName: "mappingProfileBar"
                Layout.fillWidth: true
                implicitHeight: profileRow.implicitHeight + tokens.spacingSmall * 2
                radius: tokens.cornerRadiusSmall
                color: tokens.fieldBackground
                border.color: tokens.border

                RowLayout {
                    id: profileRow
                    objectName: "mappingProfileRow"
                    anchors.fill: parent
                    anchors.margins: tokens.spacingTiny
                    spacing: tokens.spacingTiny

                    ComboBox {
                        id: mappingProfileCombo
                        objectName: "mappingProfileCombo"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 96
                        Layout.preferredWidth: 150
                        model: SettingsController.mappingProfileNames
                        currentIndex: SettingsController.activeMappingProfileIndex
                        onActivated: SettingsController.switchMappingProfile(index)
                        Accessible.name: qsTr("当前按键映射配置方案")
                    }
                    Button {
                        objectName: "createMappingProfileButton"
                        text: qsTr("＋ 新建方案")
                        leftPadding: tokens.spacingSmall
                        rightPadding: tokens.spacingSmall
                        onClicked: profileNameDialog.openForCreate()
                    }
                    Button {
                        objectName: "renameMappingProfileButton"
                        text: qsTr("重命名…")
                        leftPadding: tokens.spacingSmall
                        rightPadding: tokens.spacingSmall
                        enabled: SettingsController.canEditMappingProfile
                        onClicked: profileNameDialog.openForRename()
                    }
                    Button {
                        objectName: "deleteMappingProfileButton"
                        text: qsTr("删除")
                        leftPadding: tokens.spacingSmall
                        rightPadding: tokens.spacingSmall
                        enabled: SettingsController.canDeleteMappingProfile
                        onClicked: deleteProfileDialog.open()
                    }
                    Button {
                        // Persist the visible edits directly. If the background
                        // bridge is missing, the controller starts it so the
                        // next physical press uses this scheme immediately.
                        id: saveMappingButton
                        objectName: "saveMappingButton"
                        text: qsTr("保存当前方案")
                        leftPadding: tokens.spacingSmall
                        rightPadding: tokens.spacingSmall
                        highlighted: true
                        enabled: SettingsController.canEditMappingProfile
                        onClicked: SettingsController.saveMappings()
                    }
                }
            }

            Label {
                Layout.fillWidth: true
                visible: !SettingsController.canEditMappingProfile
                text: qsTr("系统默认方案为只读；点击“＋ 新建方案”复制后即可编辑。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }

            Rectangle {
                visible: !SettingsController.bridgeRunning
                Layout.fillWidth: true
                implicitHeight: bridgeStoppedRow.implicitHeight + tokens.spacingSmall * 2
                radius: tokens.cornerRadiusSmall
                color: tokens.surface
                border.color: tokens.errorColor
                border.width: 1

                RowLayout {
                    id: bridgeStoppedRow
                    anchors.fill: parent
                    anchors.margins: tokens.spacingSmall
                    spacing: tokens.spacingSmall

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr("桥接未运行，长按、双击和普通按键都不会生效。")
                        color: tokens.errorColor
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    Button {
                        text: qsTr("启动桥接")
                        highlighted: true
                        leftPadding: tokens.spacingSmall
                        rightPadding: tokens.spacingSmall
                        onClicked: SettingsController.ensureBridgeRunning()
                    }
                }
            }

            // -- Status / error feedback (mirrors ConnectionPage.qml) --------
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                visible: text.length > 0
                text: SettingsController.errorMessage
                color: tokens.errorColor
                font.pixelSize: tokens.fontSizeSmall
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                visible: text.length > 0 && SettingsController.errorMessage.length === 0
                text: SettingsController.statusMessage
                color: tokens.successColor
                font.pixelSize: tokens.fontSizeSmall
            }

            Rectangle {
                Layout.fillWidth: true
                implicitHeight: detectionRow.implicitHeight + tokens.spacingMedium * 2
                radius: tokens.cornerRadiusSmall
                color: tokens.fieldBackground
                border.color: SettingsController.keyDetectionActive
                    ? tokens.accent : tokens.border
                border.width: SettingsController.keyDetectionActive ? 2 : 1

                RowLayout {
                    id: detectionRow
                    anchors.fill: parent
                    anchors.margins: tokens.spacingMedium
                    spacing: tokens.spacingMedium

                    Button {
                        id: detectRealKeyButton
                        objectName: "detectRealKeyButton"
                        text: SettingsController.keyDetectionActive
                            ? qsTr("停止检测") : qsTr("检测真实按键")
                        highlighted: SettingsController.keyDetectionActive
                        onClicked: SettingsController.keyDetectionActive
                            ? SettingsController.stopKeyDetection()
                            : SettingsController.startKeyDetection()
                        Accessible.name: qsTr("检测真实遥控器按键")
                    }

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: SettingsController.keyDetectionText
                        color: SettingsController.keyDetectionActive
                            ? tokens.accent : tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }

                    Button {
                        objectName: "editTextMenuButton"
                        text: qsTr("快捷文本菜单")
                        Layout.minimumWidth: implicitWidth
                        enabled: SettingsController.canEditMappingProfile
                        onClicked: textMenuEditor.open()
                        ToolTip.visible: hovered
                        ToolTip.text: enabled
                            ? qsTr("编辑当前配置方案的快捷文本菜单")
                            : qsTr("系统默认方案为只读，请先新建方案")
                    }
                }
            }

            GridView {
                id: mappingList
                objectName: "mappingList"  // stable hook for positioning a mapping card
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: ButtonMappingModel
                cellWidth: Math.max(220, Math.floor(width / 2))
                cellHeight: 118
                currentIndex: ButtonMappingModel.indexOfButton(SettingsController.selectedButtonId)
                highlightFollowsCurrentItem: true
                onCurrentIndexChanged: positionViewAtIndex(currentIndex, GridView.Contain)

                delegate: Rectangle {
                    id: mappingRow

                    required property int index
                    required property string buttonId
                    required property string displayName
                    required property string hidUsage
                    required property string actionText
                    required property string doubleClickText
                    required property string longPressText
                    required property bool isMic
                    required property bool isSelected

                    width: mappingList.cellWidth - tokens.spacingTiny
                    height: mappingList.cellHeight - tokens.spacingTiny
                    radius: tokens.cornerRadiusSmall
                    color: isSelected
                        ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.12)
                        : "transparent"
                    border.width: isSelected ? 1 : 0
                    border.color: tokens.accent

                    // Editable QQC2 ComboBox's own internal currentIndex/
                    // editText sync (triggered during its construction, and
                    // again on every user selection) overwrites a plain
                    // declarative `editText: mappingRow.actionText` binding
                    // the moment the component finishes initializing - a
                    // well-known ComboBox(editable:true) pitfall. Setting it
                    // imperatively once after completion, then re-syncing it
                    // explicitly whenever the underlying row data changes
                    // (e.g. "恢复默认"), keeps the visible text correct
                    // without fighting ComboBox's own internal writes.
                    onActionTextChanged: actionCombo.editText = actionText
                    onDoubleClickTextChanged: doubleActionCombo.editText = doubleClickText
                    onLongPressTextChanged: longActionCombo.editText = longPressText

                    TapHandler {
                        onTapped: SettingsController.selectButton(mappingRow.buttonId)
                    }

                    RowLayout {
                        id: rowContent
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.bottom: mappingRow.isMic ? micHint.top : gestureRow.top
                        anchors.leftMargin: tokens.spacingSmall
                        anchors.rightMargin: tokens.spacingSmall
                        anchors.topMargin: tokens.spacingSmall
                        anchors.bottomMargin: tokens.spacingTiny
                        spacing: tokens.spacingTiny

                        ColumnLayout {
                            Layout.preferredWidth: 58
                            Layout.minimumWidth: 52
                            Layout.fillHeight: true
                            spacing: 0
                            Label {
                                Layout.fillWidth: true
                                text: mappingRow.displayName
                                color: tokens.textPrimary
                                font.pixelSize: tokens.fontSizeSmall
                                elide: Text.ElideRight
                            }
                            Label {
                                Layout.fillWidth: true
                                text: mappingRow.hidUsage
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                                elide: Text.ElideRight
                            }
                        }

                        ComboBox {
                            id: actionCombo
                            // Per-row name (not a fixed literal) so a test
                            // can locate one specific row's ComboBox, e.g.
                            // findChild(QObject, "actionCombo_power") - a
                            // real, stable hook, not a fake production
                            // behavior.
                            objectName: "actionCombo_" + mappingRow.buttonId
                            visible: !mappingRow.isMic
                            enabled: SettingsController.canEditMappingProfile
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            editable: true
                            model: SettingsController.presetActionOptions
                            delegate: ItemDelegate {
                                width: actionCombo.width
                                text: modelData
                                enabled: !root.isActionCategory(modelData)
                                font.bold: root.isActionCategory(modelData)
                            }
                            Accessible.name: mappingRow.displayName
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("可直接输入任意单键或组合键，例如 f8、ctrl+shift+p；输入“禁用”可关闭此键。")

                            // Guards onEditTextChanged below against the
                            // SAME construction-time noise
                            // Component.onCompleted works around (ComboBox's
                            // own internal currentIndex-driven editText sync
                            // fires during construction, before this flag is
                            // set true) - without it, every row would
                            // immediately persist its transient default
                            // ("escape", index 0 of presetActionOptions)
                            // into the model the instant it's created,
                            // reintroducing the "all rows show/save escape"
                            // bug this task's own screenshot step caught
                            // earlier.
                            property bool _initialized: false

                            // NOT `editText: mappingRow.actionText` (a
                            // plain declarative binding) - ComboBox's own
                            // internal currentIndex/editText sync overwrites
                            // that the moment construction finishes (a
                            // well-known ComboBox(editable:true) pitfall).
                            // Set imperatively here instead, strictly AFTER
                            // that internal sync has already run.
                            Component.onCompleted: {
                                editText = mappingRow.actionText
                                _initialized = true
                            }

                            // XRBM-030 RETRY 1 blocker 1: onAccepted (Enter)
                            // and onActivated (picking a dropdown item)
                            // alone are not enough - a user who types a
                            // custom chord (e.g. "ctrl+shift+p") and clicks
                            // a SAVE button without ever pressing Enter
                            // previously left the model (and therefore
                            // _save()'s persisted binding) holding the OLD
                            // value, silently discarding the typed edit.
                            // Committing on every live editText change closes
                            // that gap unconditionally - by the time any
                            // save action runs, the model already holds
                            // whatever is visibly displayed, with no
                            // separate "commit on blur/save" event to miss.
                            // Guarded by _initialized (see above) so this
                            // never fires from ComboBox's own construction-
                            // time internal writes, only from a real,
                            // post-construction edit (by typing or by
                            // restoreMappingDefaults()/onActionTextChanged
                            // resetting editText, which harmlessly re-writes
                            // the model with the exact same value it already
                            // has).
                            onEditTextChanged: {
                                if (_initialized) {
                                    ButtonMappingModel.setActionTextAt(mappingRow.index, editText)
                                }
                            }

                            // Kept for defense-in-depth / clarity of intent
                            // even though onEditTextChanged above already
                            // covers both cases (accepting Enter, or picking
                            // a dropdown item, both change editText too).
                            onAccepted: ButtonMappingModel.setActionTextAt(mappingRow.index, editText)
                            onActivated: {
                                if (!root.isActionCategory(currentText))
                                    ButtonMappingModel.setActionTextAt(mappingRow.index, currentText)
                            }
                        }

                        Label {
                            id: voiceHotkeySummary
                            objectName: "voiceHotkeySummary_" + mappingRow.buttonId
                            visible: mappingRow.isMic
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            text: qsTr("语音快捷键：") + SettingsController.hotkeyText
                                  + qsTr("（在“连接”页设置）")
                            color: tokens.textSecondary
                            elide: Text.ElideRight
                            Accessible.name: text
                        }

                        Button {
                            objectName: "editTextAction_" + mappingRow.buttonId
                            visible: !mappingRow.isMic
                                && root.isTextSubmitAction(actionCombo.editText)
                            enabled: SettingsController.canEditMappingProfile
                            text: qsTr("文")
                            Layout.preferredWidth: 34
                            Layout.minimumWidth: 30
                            onClicked: root.openTextSubmitEditor(
                                mappingRow.index, "single_click", actionCombo.editText
                            )
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("编辑要输入的文本（不按回车）")
                            Accessible.name: qsTr("编辑") + mappingRow.displayName + qsTr("的输入文本")
                        }

                        Button {
                            objectName: "recordShortcut_" + mappingRow.buttonId
                            visible: !mappingRow.isMic
                            enabled: SettingsController.canEditMappingProfile
                            text: qsTr("录")
                            Layout.preferredWidth: 34
                            Layout.minimumWidth: 30
                            onClicked: root.openShortcutRecorder(
                                mappingRow.buttonId, mappingRow.index,
                                mappingRow.isMic, "single_click"
                            )
                            Accessible.name: qsTr("录制") + mappingRow.displayName + qsTr("快捷键")
                        }
                    }

                    Label {
                        id: micHint
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.leftMargin: tokens.spacingSmall
                        anchors.rightMargin: tokens.spacingSmall
                        anchors.bottomMargin: tokens.spacingSmall
                        visible: mappingRow.isMic
                        text: qsTr("RC003 麦克风键需要按住才能持续传送声音")
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                        elide: Text.ElideRight
                    }

                    RowLayout {
                        id: gestureRow
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.leftMargin: tokens.spacingSmall
                        anchors.rightMargin: tokens.spacingSmall
                        anchors.bottomMargin: tokens.spacingSmall
                        visible: !mappingRow.isMic
                        height: 38
                        spacing: tokens.spacingTiny

                        Label {
                            text: qsTr("双")
                            color: tokens.textSecondary
                            font.pixelSize: tokens.fontSizeSmall
                        }
                        ComboBox {
                            id: doubleActionCombo
                            objectName: "doubleActionCombo_" + mappingRow.buttonId
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            editable: true
                            enabled: SettingsController.canEditMappingProfile
                            model: SettingsController.presetActionOptions
                            delegate: ItemDelegate {
                                width: doubleActionCombo.width
                                text: modelData
                                enabled: !root.isActionCategory(modelData)
                                font.bold: root.isActionCategory(modelData)
                            }
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("双击动作；配置后等待约 0.3 秒区分单击和双击")
                            property bool _initialized: false
                            Component.onCompleted: {
                                editText = mappingRow.doubleClickText
                                _initialized = true
                            }
                            onEditTextChanged: {
                                if (_initialized)
                                    ButtonMappingModel.setSecondaryActionTextAt(
                                        mappingRow.index, "double_click", editText
                                    )
                            }
                            onAccepted: ButtonMappingModel.setSecondaryActionTextAt(
                                mappingRow.index, "double_click", editText
                            )
                            onActivated: {
                                if (!root.isActionCategory(currentText))
                                    ButtonMappingModel.setSecondaryActionTextAt(
                                        mappingRow.index, "double_click", currentText
                                    )
                            }
                        }
                        Button {
                            objectName: "editDoubleTextAction_" + mappingRow.buttonId
                            visible: root.isTextSubmitAction(doubleActionCombo.editText)
                            enabled: SettingsController.canEditMappingProfile
                            text: qsTr("文")
                            Layout.preferredWidth: 30
                            Layout.minimumWidth: 28
                            onClicked: root.openTextSubmitEditor(
                                mappingRow.index, "double_click", doubleActionCombo.editText
                            )
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("编辑双击时输入的文本")
                            Accessible.name: qsTr("编辑双击输入文本")
                        }
                        Button {
                            objectName: "recordDoubleShortcut_" + mappingRow.buttonId
                            enabled: SettingsController.canEditMappingProfile
                            text: qsTr("录")
                            Layout.preferredWidth: 30
                            Layout.minimumWidth: 28
                            onClicked: root.openShortcutRecorder(
                                mappingRow.buttonId, mappingRow.index,
                                mappingRow.isMic, "double_click"
                            )
                            Accessible.name: qsTr("录制双击") + mappingRow.displayName
                        }
                        Label {
                            text: qsTr("长")
                            color: tokens.textSecondary
                            font.pixelSize: tokens.fontSizeSmall
                        }
                        ComboBox {
                            id: longActionCombo
                            objectName: "longActionCombo_" + mappingRow.buttonId
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            editable: true
                            enabled: SettingsController.canEditMappingProfile
                            model: SettingsController.presetActionOptions
                            delegate: ItemDelegate {
                                width: longActionCombo.width
                                text: modelData
                                enabled: !root.isActionCategory(modelData)
                                font.bold: root.isActionCategory(modelData)
                            }
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("长按动作；按住约 0.55 秒触发并抑制单击")
                            property bool _initialized: false
                            Component.onCompleted: {
                                editText = mappingRow.longPressText
                                _initialized = true
                            }
                            onEditTextChanged: {
                                if (_initialized)
                                    ButtonMappingModel.setSecondaryActionTextAt(
                                        mappingRow.index, "long_press", editText
                                    )
                            }
                            onAccepted: ButtonMappingModel.setSecondaryActionTextAt(
                                mappingRow.index, "long_press", editText
                            )
                            onActivated: {
                                if (!root.isActionCategory(currentText))
                                    ButtonMappingModel.setSecondaryActionTextAt(
                                        mappingRow.index, "long_press", currentText
                                    )
                            }
                        }
                        Button {
                            objectName: "editLongTextAction_" + mappingRow.buttonId
                            visible: root.isTextSubmitAction(longActionCombo.editText)
                            enabled: SettingsController.canEditMappingProfile
                            text: qsTr("文")
                            Layout.preferredWidth: 30
                            Layout.minimumWidth: 28
                            onClicked: root.openTextSubmitEditor(
                                mappingRow.index, "long_press", longActionCombo.editText
                            )
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("编辑长按时输入的文本")
                            Accessible.name: qsTr("编辑长按输入文本")
                        }
                        Button {
                            objectName: "recordLongShortcut_" + mappingRow.buttonId
                            enabled: SettingsController.canEditMappingProfile
                            text: qsTr("录")
                            Layout.preferredWidth: 30
                            Layout.minimumWidth: 28
                            onClicked: root.openShortcutRecorder(
                                mappingRow.buttonId, mappingRow.index,
                                mappingRow.isMic, "long_press"
                            )
                            Accessible.name: qsTr("录制长按") + mappingRow.displayName
                        }
                    }
                }
            }
        }
    }

    RowLayout {
        id: djiControlLayout
        objectName: "djiControlLayout"
        visible: SettingsController.isDjiMic2Device
        anchors.fill: parent
        anchors.margins: tokens.spacingLarge
        spacing: tokens.spacingLarge

        ColumnLayout {
            Layout.preferredWidth: 250
            Layout.fillHeight: true
            spacing: tokens.spacingSmall

            Rectangle {
                Layout.preferredWidth: 210
                Layout.preferredHeight: 360
                Layout.alignment: Qt.AlignHCenter | Qt.AlignTop
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1

                Rectangle {
                    id: transmitter
                    width: 112
                    height: 250
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    anchors.topMargin: 32
                    radius: tokens.cornerRadiusLarge
                    color: tokens.fieldBackground
                    border.color: tokens.border
                    border.width: 1

                    Label {
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 22
                        text: qsTr("DJI MIC 2")
                        font.bold: true
                        color: tokens.textPrimary
                    }

                    Rectangle {
                        width: 36
                        height: 36
                        radius: 18
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 72
                        color: tokens.surface
                        border.color: tokens.voiceAccent
                        border.width: 2
                        Label {
                            anchors.centerIn: parent
                            text: qsTr("录")
                            color: tokens.textPrimary
                            font.bold: true
                        }
                    }

                    Rectangle {
                        width: 64
                        height: 32
                        radius: tokens.cornerRadiusSmall
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 132
                        color: tokens.surface
                        border.color: tokens.border
                        Label {
                            anchors.centerIn: parent
                            text: qsTr("连接")
                            color: tokens.textPrimary
                        }
                    }

                    Rectangle {
                        width: 64
                        height: 32
                        radius: tokens.cornerRadiusSmall
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 184
                        color: tokens.surface
                        border.color: tokens.border
                        Label {
                            anchors.centerIn: parent
                            text: qsTr("电源")
                            color: tokens.textPrimary
                        }
                    }
                }

                Label {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 22
                    text: qsTr("功能示意，非产品照片")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                }
            }

            Label {
                Layout.preferredWidth: 230
                Layout.alignment: Qt.AlignHCenter
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                text: qsTr("DJI Mic 2 在 Windows 中首先是录音输入设备，不继承 RC003 的 13 键映射。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: tokens.spacingSmall

            Label {
                text: qsTr("DJI Mic 2 设备控制")
                font.pixelSize: tokens.fontSizeTitle
                font.bold: true
                color: tokens.textPrimary
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("当前可自定义映射：0。只有在真实 Windows 捕获到某个实体键的独立输入事件后，才会开放该键的映射选项。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }

            Repeater {
                model: SettingsController.djiControlRows
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: controlRow.implicitHeight + tokens.spacingMedium * 2
                    radius: tokens.cornerRadiusSmall
                    color: tokens.surface
                    border.color: tokens.border
                    border.width: 1

                    RowLayout {
                        id: controlRow
                        anchors.fill: parent
                        anchors.margins: tokens.spacingMedium
                        spacing: tokens.spacingMedium

                        Rectangle {
                            Layout.preferredWidth: 72
                            Layout.preferredHeight: 32
                            radius: tokens.cornerRadiusSmall
                            color: tokens.fieldBackground
                            Label {
                                anchors.centerIn: parent
                                text: modelData.name
                                color: tokens.textPrimary
                                font.bold: true
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: modelData.behavior
                                color: tokens.textPrimary
                                font.pixelSize: tokens.fontSizeBody
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: modelData.mapping
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                        }
                        Label {
                            text: qsTr("硬件内置")
                            color: tokens.disabledText
                            font.pixelSize: tokens.fontSizeSmall
                        }
                    }
                }
            }

            Item { Layout.fillHeight: true }

            RowLayout {
                Layout.alignment: Qt.AlignRight
                Button {
                    text: qsTr("重新检测麦克风")
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
}
