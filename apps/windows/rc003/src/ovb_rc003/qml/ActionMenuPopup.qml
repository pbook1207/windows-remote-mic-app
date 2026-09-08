import QtQuick
import QtQuick.Controls

// Shared popup for every action ComboBox on the mapping page.  The stock
// FluentWinUI3 popup inherits the width of its ComboBox, which makes the
// single/double/long-press menus three different widths and truncates the
// longer Chinese action names.  It can also stop between rows when its
// height is constrained by the window.  Keep one predictable, scrollable
// geometry instead.
Popup {
    id: menu

    required property var control
    property real menuWidth: 320
    property real rowHeight: 42
    property int maximumVisibleRows: 11

    x: 0
    y: control.height
    width: menuWidth
    height: Math.min(
        menuList.contentHeight + topPadding + bottomPadding,
        maximumVisibleRows * rowHeight + topPadding + bottomPadding
    )

    topMargin: 8
    bottomMargin: 8
    leftMargin: 8
    rightMargin: 8
    topPadding: 4
    bottomPadding: 4
    leftPadding: 4
    rightPadding: 4

    contentItem: ListView {
        id: menuList
        clip: true
        implicitHeight: contentHeight
        model: menu.control.delegateModel
        currentIndex: menu.control.highlightedIndex
        highlightMoveDuration: 0
        boundsBehavior: Flickable.StopAtBounds
        snapMode: ListView.SnapToItem

        ScrollBar.vertical: ScrollBar {
            policy: ScrollBar.AsNeeded
        }
    }

    onOpened: {
        if (menuList.currentIndex >= 0)
            menuList.positionViewAtIndex(menuList.currentIndex, ListView.Contain)
        menuList.returnToBounds()
    }
}
