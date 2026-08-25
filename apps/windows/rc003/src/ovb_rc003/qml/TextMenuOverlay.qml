import QtQuick
import QtQuick.Window

Window {
    id: root
    width: 96
    height: Math.min(236, 12 + menuController.items.length * 28)
    x: menuController.panelX
    y: menuController.panelY
    visible: menuController.visible
    color: "transparent"
    title: "快捷文本"
    flags: Qt.FramelessWindowHint
           | Qt.WindowStaysOnTopHint
           | Qt.Tool
           | Qt.WindowDoesNotAcceptFocus

    readonly property string uiFont: "Microsoft YaHei UI"
    readonly property color primaryText: "#242424"

    Rectangle {
        anchors.fill: parent
        anchors.margins: 4
        y: 3
        radius: 10
        color: "#24000000"
    }

    Rectangle {
        anchors.fill: parent
        anchors.margins: 2
        radius: 9
        color: "#FFFFFF"
        border.color: "#E7E7E7"
        border.width: 1

        ListView {
            id: list
            anchors.fill: parent
            anchors.margins: 4
            model: menuController.items
            interactive: false
            spacing: 1
            clip: true
            currentIndex: menuController.selectedIndex

            onCurrentIndexChanged: positionViewAtIndex(currentIndex, ListView.Contain)

            delegate: Rectangle {
                required property int index
                required property var modelData
                width: list.width
                height: 27
                color: index === menuController.selectedIndex || itemMouse.containsMouse
                       ? "#F0F0F0" : "transparent"

                MouseArea {
                    id: itemMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    acceptedButtons: Qt.LeftButton
                    cursorShape: Qt.PointingHandCursor
                    onClicked: menuController.selectItem(index)
                }

                Text {
                    anchors.left: parent.left
                    anchors.leftMargin: 18
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    text: modelData.label
                    color: root.primaryText
                    elide: Text.ElideRight
                    font.family: root.uiFont
                    font.pixelSize: 12
                    font.weight: Font.Normal
                    font.hintingPreference: Font.PreferFullHinting
                    renderType: Text.NativeRendering
                }
            }
        }
    }
}
